import torch
from typing import Tuple, TypedDict, Callable

import comfy.model_management
from comfy.model_patcher import ModelPatcher
from comfy.sd import VAE
from comfy.ldm.models.autoencoder import AutoencoderKL
from comfy.model_base import BaseModel
from nodes import VAEEncode


class UnetParams(TypedDict):
    input: torch.Tensor
    timestep: torch.Tensor
    c: dict
    cond_or_uncond: torch.Tensor


class VAEEncodeArgMax(VAEEncode):
    """Setting regularizer.sample = False to obtain mode of distribution."""

    def encode(self, vae: VAE, pixels):
        """@Override"""
        assert isinstance(
            vae.first_stage_model, AutoencoderKL
        ), "ArgMax only supported for AutoencoderKL"
        original_sample_mode = vae.first_stage_model.regularization.sample
        try:
            vae.first_stage_model.regularization.sample = False
            ret = super().encode(vae, pixels)
        finally:
            vae.first_stage_model.regularization.sample = original_sample_mode
        return ret


class ICLightApplyMaskGrey:
    """Make masked area grey(127) in the image."""

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "image": ("IMAGE",),
                "alpha": ("MASK",),
            }
        }

    CATEGORY = "mask/compositing"
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "apply_mask"

    def apply_mask(self, image: torch.Tensor, alpha: torch.Tensor):
        if not isinstance(alpha, torch.Tensor):
            raise TypeError("Expected alpha to be a torch.Tensor")
        if alpha.ndim == 3:
            # [B, H, W] => [B, H, W, C=1]
            alpha = alpha.unsqueeze(-1)
        result = image * alpha + (1 - alpha) * 0.5
        return (result,)


class ICLight:
    """ICLightImpl"""

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL",),
                "ic_model": ("MODEL",),
                "c_concat": ("LATENT",),
            },
        }

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "apply"
    CATEGORY = "_for_testing"

    def apply(
        self,
        model: ModelPatcher,
        ic_model: ModelPatcher,
        c_concat: dict,
    ) -> Tuple[ModelPatcher]:
        if "samples" not in c_concat:
            raise KeyError("Expected key 'samples' in c_concat")

        device = comfy.model_management.get_torch_device()
        dtype = comfy.model_management.unet_dtype()
        work_model = model.clone()

        # Apply scale factor.
        base_model: BaseModel = work_model.model
        scale_factor = base_model.model_config.latent_format.scale_factor
        # [B, 4, H, W]
        concat_conds: torch.Tensor = c_concat["samples"] * scale_factor
        # [1, 4 * B, H, W]
        concat_conds = torch.cat([c[None, ...] for c in concat_conds], dim=1)

        def apply_c_concat(params: UnetParams) -> UnetParams:
            """Apply c_concat on unet call."""
            sample = params["input"]
            params["c"]["c_concat"] = torch.cat(
                (
                    [concat_conds.to(sample.device)]
                    * (sample.shape[0] // concat_conds.shape[0])
                ),
                dim=0,
            )
            return params

        def unet_dummy_apply(unet_apply: Callable, params: UnetParams):
            """A dummy unet apply wrapper serving as the endpoint of wrapper
            chain."""
            return unet_apply(x=params["input"], t=params["timestep"], **params["c"])

        # Compose on existing `model_function_wrapper`.
        existing_wrapper = work_model.model_options.get(
            "model_function_wrapper", unet_dummy_apply
        )

        def wrapper_func(unet_apply: Callable, params: UnetParams):
            return existing_wrapper(unet_apply, params=apply_c_concat(params))

        work_model.set_model_unet_function_wrapper(wrapper_func)

        ic_model_state_dict = {
            key: value.to(dtype=dtype, device=device)
            for key, value in ic_model.model.diffusion_model.state_dict().items()
        }
        work_model.add_patches(
            patches={
                ("diffusion_model." + key): (
                    "diff",
                    [
                        ic_model_state_dict[key],
                        {"pad_weight": key == "input_blocks.0.0.weight"},
                    ],
                )
                for key in ic_model_state_dict
            }
        )

        # release memory usage
        ic_model_state_dict.clear()
        del ic_model_state_dict
        torch.cuda.empty_cache()
        comfy.model_management.soft_empty_cache()

        return (work_model,)


NODE_CLASS_MAPPINGS = {
    "ICLightApply": ICLight,
    "ICLightApplyMaskGrey": ICLightApplyMaskGrey,
    "VAEEncodeArgMax": VAEEncodeArgMax,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ICLightApply": "IC Light Apply",
    "ICLightApplyMaskGrey": "IC Light Apply Mask Grey",
    "VAEEncodeArgMax": "VAE Encode ArgMax",
}
