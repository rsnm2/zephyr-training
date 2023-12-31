"""Implements a HF DPO Model wrapped in :class:`.ComposerModel`."""

import logging, os, warnings, random
from typing import Mapping, Union, Union, Dict, Optional, Tuple, List

import torch
import torch.nn.functional as F

from transformers import PreTrainedModel, AutoConfig, AutoModelForCausalLM, PreTrainedTokenizerBase
from trl.trainer.utils import pad_to_length

from omegaconf import DictConfig
from composer.core import Event, Algorithm
from composer.models.huggingface import HuggingFaceModel
from composer.trainer.dist_strategy import prepare_fsdp_module
from composer.utils import dist, get_device

from llmfoundry.models.hf.hf_fsdp import hf_get_init_device, prepare_hf_model_for_fsdp
from llmfoundry.models.layers.attention import is_flash_v2_installed
from llmfoundry.models.utils import init_empty_weights

log = logging.getLogger(__name__)

_HF_IGNORE_INDEX = -100
_PADDING_VALUE = 0
_LOSS_TYPE = "sigmoid"

class DPO(Algorithm):
    def __init__(self, ref_model, beta=0.1):
        self.ref_model = ref_model
        self.beta = beta
        self.first_time = True
        self.loss_type = _LOSS_TYPE
    
    def match(self, event, state):
        """
        Event.AFTER_FORWARD = compute loss 
        Event.FIT_START = initialize FSDP for teacher model
        """
        return event == Event.AFTER_LOSS or event == Event.FIT_START

    def apply(self, event, state, logger):
        if event == Event.FIT_START and self.first_time:
            self.first_time = False 
            if torch.distributed.get_world_size() > 1:
                prepare_fsdp_module(
                    model=self.ref_model,
                    optimizers=None,
                    fsdp_config=state.fsdp_config,
                    precision=state.precision,
                    device=get_device(None),
                    auto_microbatching=False,
                )
            else:
                self.ref_model = self.ref_model.to("cuda")
        
        elif event == Event.AFTER_LOSS:
            # ref model forward
            with torch.no_grad():
                (
                    ref_chosen_logps, 
                    ref_rejected_logps, 
                    _, 
                    _
                ) = self.ref_model(state.batch)
            
            # sft model forward (from forward pass)
            (
                policy_chosen_logps,
                policy_rejected_logps,
                policy_chosen_logits,
                policy_rejected_logits,
            ) = state.outputs

            # compute DPO loss
            losses, chosen_rewards, rejected_rewards = self.dpo_loss(
                policy_chosen_logps,
                policy_rejected_logps,
                ref_chosen_logps,
                ref_rejected_logps,
            )

            loss = losses.mean()


            metrics = {}
            metrics[f"dpo/logps/rejected"] = policy_rejected_logps.detach().cpu().mean()
            metrics[f"dpo/logps/chosen"] = policy_chosen_logps.detach().cpu().mean()
            logger.log_metrics(metrics)

            state.loss += loss
    
    # adapted from https://github.com/huggingface/trl/blob/main/trl/trainer/dpo_trainer.py#L416C18-L416C18
    def dpo_loss(
        self,
        policy_chosen_logps: torch.FloatTensor,
        policy_rejected_logps: torch.FloatTensor,
        reference_chosen_logps: torch.FloatTensor,
        reference_rejected_logps: torch.FloatTensor,
    ) -> Tuple[torch.FloatTensor, torch.FloatTensor, torch.FloatTensor]:
        """Compute the DPO loss for a batch of policy and reference model log probabilities.

        Args:
            policy_chosen_logps: Log probabilities of the policy model for the chosen responses. Shape: (batch_size,)
            policy_rejected_logps: Log probabilities of the policy model for the rejected responses. Shape: (batch_size,)
            reference_chosen_logps: Log probabilities of the reference model for the chosen responses. Shape: (batch_size,)
            reference_rejected_logps: Log probabilities of the reference model for the rejected responses. Shape: (batch_size,)
            beta: Temperature parameter for the DPO loss, typically something in the range of 0.1 to 0.5. We ignore the reference model as beta -> 0.
            reference_free: If True, we ignore the _provided_ reference model and implicitly use a reference model that assigns equal probability to all responses.

        Returns:
            A tuple of three tensors: (losses, chosen_rewards, rejected_rewards).
            The losses tensor contains the DPO loss for each example in the batch.
            The chosen_rewards and rejected_rewards tensors contain the rewards for the chosen and rejected responses, respectively.
        """
        pi_logratios = policy_chosen_logps - policy_rejected_logps
        ref_logratios = reference_chosen_logps - reference_rejected_logps

        logits = pi_logratios - ref_logratios

        if self.loss_type == "sigmoid":
            losses = -F.logsigmoid(self.beta * logits)
        elif self.loss_type == "hinge":
            losses = torch.relu(1 - self.beta * logits)
        else:
            raise ValueError(f"Unknown loss type: {self.loss_type}. Should be one of ['sigmoid', 'hinge']")

        chosen_rewards = self.beta * (policy_chosen_logps - reference_chosen_logps).detach()
        rejected_rewards = self.beta * (policy_rejected_logps - reference_rejected_logps).detach()

        return losses, chosen_rewards, rejected_rewards

class HuggingFaceModelWithDPO(HuggingFaceModel):
    def __init__(self,
                 model: PreTrainedModel,
                 tokenizer: Optional[PreTrainedTokenizerBase] = None,
                 shift_labels: bool = False,
                 init_device: Optional[str] = None,
                 beta: float = 0.1):
        super().__init__(model,
                         tokenizer,
                         use_logits=True,
                         shift_labels=shift_labels)

        self.loss_type = _LOSS_TYPE
        self.beta = beta

        # Note: We need to add the FSDP related attributes to the model AFTER the super init,
        # so that the (possible) embedding resizing doesn't destroy them
        prepare_hf_model_for_fsdp(self.model, init_device)

        # support for meta initialization
        self.model.param_init_fn = lambda module: self.model._init_weights(
            module)

    # override forward to use call dpo related function
    def forward(self, batch: Mapping):
        if isinstance(batch, dict) or isinstance(batch, UserDict):
            output = self.concatenated_forward(batch)
        else:
            raise ValueError('Unexpected batch type')
        return output

    def loss(self, outputs, batch):
        # loss calculated in the Algorithm in Event.AFTER_LOSS
        return 0.
    
    # adapted from: https://github.com/huggingface/trl/blob/main/trl/trainer/dpo_trainer.py#L493
    def concatenated_forward(self, batch: Dict[str, Union[List, torch.LongTensor]]):
        """Run the given model on the given batch of inputs, concatenating the chosen and rejected inputs together.
        We do this to avoid doing two forward passes, because it's faster for FSDP.
        """
        concatenated_batch = self.concatenate_inputs(batch)
        len_chosen = batch["chosen_labels"].shape[0]

        all_logits = self.model(
            concatenated_batch["concatenated_input_ids"],
            attention_mask=concatenated_batch["concatenated_attention_mask"],
        ).logits.to(torch.float32)

        all_logps = self._get_batch_logps(
            all_logits,
            concatenated_batch["concatenated_labels"],
            average_log_prob=False,
        )

        chosen_logps = all_logps[:len_chosen]
        rejected_logps = all_logps[len_chosen:]

        chosen_logits = all_logits[:len_chosen]
        rejected_logits = all_logits[len_chosen:]

        return (chosen_logps, rejected_logps, chosen_logits, rejected_logits)

    # adapted from: https://github.com/huggingface/trl/blob/main/trl/trainer/dpo_trainer.py#L377C11-L377C11
    def concatenate_inputs(self, batch):
        """Concatenate the chosen and rejected inputs into a single tensor.
        Args:
            batch: A batch of data. Must contain the keys 'chosen_input_ids' and 'rejected_input_ids', which are tensors of shape (batch_size, sequence_length).
        Returns:
            A dictionary containing the concatenated inputs under the key 'concatenated_input_ids'.
        """
        concatenated_batch = {}
        max_length = max(batch["chosen_input_ids"].shape[1], batch["rejected_input_ids"].shape[1])

        for k in batch:
            if k.startswith("chosen") and isinstance(batch[k], torch.Tensor):
                pad_value = _HF_IGNORE_INDEX if "labels" in k else _PADDING_VALUE
                concatenated_key = k.replace("chosen", "concatenated")
                concatenated_batch[concatenated_key] = pad_to_length(batch[k], max_length, pad_value=pad_value)
        for k in batch:
            if k.startswith("rejected") and isinstance(batch[k], torch.Tensor):
                pad_value = _HF_IGNORE_INDEX if "labels" in k else _PADDING_VALUE
                concatenated_key = k.replace("rejected", "concatenated")
                concatenated_batch[concatenated_key] = torch.cat(
                    (
                        concatenated_batch[concatenated_key],
                        pad_to_length(batch[k], max_length, pad_value=pad_value),
                    ),
                    dim=0,
                )
        return concatenated_batch

    # adapated from https://github.com/huggingface/trl/blob/main/trl/trainer/dpo_trainer.py#L459C5-L491C57
    def _get_batch_logps(
        self,
        logits: torch.FloatTensor,
        labels: torch.LongTensor,
        average_log_prob: bool = False):

        """Compute the log probabilities of the given labels under the given logits.
        Args:
            logits: Logits of the model (unnormalized). Shape: (batch_size, sequence_length, vocab_size)
            labels: Labels for which to compute the log probabilities. Label tokens with a value of label_pad_token_id are ignored. Shape: (batch_size, sequence_length)
            average_log_prob: If True, return the average log probability per (non-masked) token. Otherwise, return the sum of the log probabilities of the (non-masked) tokens.
        Returns:
            A tensor of shape (batch_size,) containing the average/sum log probabilities of the given labels under the given logits.
        """

        if logits.shape[:-1] != labels.shape:
            raise ValueError("Logits (batch and sequence length dim) and labels must have the same shape.")

        labels = labels[:, 1:].clone()
        logits = logits[:, :-1, :]
        loss_mask = labels != _HF_IGNORE_INDEX

        # dummy token; we'll ignore the losses on these tokens later
        labels[labels == _HF_IGNORE_INDEX] = 0

        per_token_logps = torch.gather(logits.log_softmax(-1), dim=2, index=labels.unsqueeze(2)).squeeze(2)

        if average_log_prob:
            return (per_token_logps * loss_mask).sum(-1) / loss_mask.sum(-1)
        else:
            return (per_token_logps * loss_mask).sum(-1)

class ComposerHFCausalLMWithDPO(HuggingFaceModelWithDPO):
    """Configures a :class:`.HuggingFaceModel` around a Causal LM.

    Args:
        om_model_config (DictConfig: either an omegaconf dictionary used to configure the mode
        if DictConfig, the following keys are required:
            cfg.pretrained_model_name_or_path (str)
            cfg.beta
        tokenizer (PreTrainedTokenizer): The tokenizer that the model will use.
    """
    def __init__(self, 
                 om_model_config: DictConfig,                               
                 tokenizer: PreTrainedTokenizerBase):

        init_device = "cpu"
        beta = om_model_config.get('beta', 0.1)

        # if we are passed a DictConfig, we need to instantiate the model
        if isinstance(om_model_config, DictConfig):
            trust_remote_code = om_model_config.get('trust_remote_code', True)
            use_auth_token = om_model_config.get('use_auth_token', False)
            use_flash_attention_2 = om_model_config.get('use_flash_attention_2', False)
            if use_flash_attention_2 and not is_flash_v2_installed():
                raise ValueError(
                    'use_flash_attention_2 is set to True, but flash-attention 2 is not installed. '
                    + 'Please install flash_attn==2.3.2`.')

            config = AutoConfig.from_pretrained(
                om_model_config.pretrained_model_name_or_path,
                trust_remote_code=trust_remote_code,
                use_auth_token=use_auth_token,
            )

            # This is not how you are supposed to set this, but transformers currently only
            # supports enabling flash attention 2 when using the from_pretrained API.
            # We need to support it for both from_pretrained and from_config, so we have to
            # set the private attribute here. This will just skip all of transformers'
            # validation logic that it is ok to use flash attention 2, so we check
            # whether it is installed above, and whether the chosen config supports it here.
            # https://github.com/huggingface/transformers/issues/26878
            config._flash_attn_2_enabled = use_flash_attention_2

            # If the HuggingFace model is coming from a local folder, Hugging Face copies the modules into the
            # transformers modules cache. On particular systems, this operation seems to cause contention between
            # the different processes. To avoid this contention, we first create the model (on meta device) on local rank
            # zero. This will set up the transformers model cache and avoid the future contention.
            if dist.get_local_rank() == 0 and os.path.isdir(
                    om_model_config.pretrained_model_name_or_path):
                with init_empty_weights(include_buffers=False):
                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore', UserWarning)
                        AutoModelForCausalLM.from_pretrained(
                            om_model_config.pretrained_model_name_or_path,
                            trust_remote_code=trust_remote_code,
                            use_auth_token=use_auth_token,
                            config=config,
                        )

            dist.barrier()

            # init model
            model = AutoModelForCausalLM.from_pretrained(
                om_model_config.pretrained_model_name_or_path,
                trust_remote_code=trust_remote_code,
                use_auth_token=use_auth_token,
                config=config)

            signal_file_path = f'.node_{dist.get_node_rank()}_local_rank0_completed'
            if dist.get_local_rank() == 0:
                with open(signal_file_path, 'wb') as f:
                    f.write(b'local_rank0_completed_download')

            # Avoid the collective call until the local rank zero has finished trying to download the checkpoint
            # so that we don't timeout for large downloads. This syncs all processes on the node
            with dist.local_rank_zero_download_and_wait(signal_file_path):
                # Then, wait to ensure every node has finished downloading the checkpoint
                dist.barrier()

            if dist.get_local_rank() == 0:
                os.remove(signal_file_path)

        # else, unsupported type
        else:
            raise ValueError(
                f'om_model_config must be either a DictConfig but got {type(om_model_config)}'
            )

        composer_model = super().__init__(model=model,
                                          shift_labels=True,
                                          tokenizer=tokenizer,
                                          beta=beta,
                                          init_device="cpu")

        return composer_model
