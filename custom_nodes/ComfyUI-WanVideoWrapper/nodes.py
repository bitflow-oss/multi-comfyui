import os
import torch
import gc
from .utils import log, print_memory, apply_lora
import numpy as np
import math
from tqdm import tqdm

from .wanvideo.modules.clip import CLIPModel
from .wanvideo.modules.model import WanModel, rope_params
from .wanvideo.modules.t5 import T5EncoderModel
from .wanvideo.utils.fm_solvers import (FlowDPMSolverMultistepScheduler,
                               get_sampling_sigmas, retrieve_timesteps)
from .wanvideo.utils.fm_solvers_unipc import FlowUniPCMultistepScheduler
from diffusers.schedulers import FlowMatchEulerDiscreteScheduler

from .enhance_a_video.globals import enable_enhance, disable_enhance, set_enhance_weight, set_num_frames
from .taehv import TAEHV

from accelerate import init_empty_weights
from accelerate.utils import set_module_tensor_to_device

import folder_paths
import comfy.model_management as mm
from comfy.utils import load_torch_file, save_torch_file, ProgressBar, common_upscale
import comfy.model_base
import comfy.latent_formats
from comfy.clip_vision import clip_preprocess, ClipVisionModel
from comfy.sd import load_lora_for_models

script_directory = os.path.dirname(os.path.abspath(__file__))

def add_noise_to_reference_video(image, ratio=None):
    sigma = torch.ones((image.shape[0],)).to(image.device, image.dtype) * ratio 
    image_noise = torch.randn_like(image) * sigma[:, None, None, None]
    image_noise = torch.where(image==-1, torch.zeros_like(image), image_noise)
    image = image + image_noise
    return image

class WanVideoBlockSwap:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "blocks_to_swap": ("INT", {"default": 20, "min": 0, "max": 40, "step": 1, "tooltip": "Number of transformer blocks to swap, the 14B model has 40, while the 1.3B model has 30 blocks"}),
                "offload_img_emb": ("BOOLEAN", {"default": False, "tooltip": "Offload img_emb to offload_device"}),
                "offload_txt_emb": ("BOOLEAN", {"default": False, "tooltip": "Offload time_emb to offload_device"}),
            },
            "optional": {
                "use_non_blocking": ("BOOLEAN", {"default": True, "tooltip": "Use non-blocking memory transfer for offloading, reserves more RAM but is faster"}),
            },
        }
    RETURN_TYPES = ("BLOCKSWAPARGS",)
    RETURN_NAMES = ("block_swap_args",)
    FUNCTION = "setargs"
    CATEGORY = "WanVideoWrapper"
    DESCRIPTION = "Settings for block swapping, reduces VRAM use by swapping blocks to CPU memory"

    def setargs(self, **kwargs):
        return (kwargs, )

class WanVideoVRAMManagement:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "offload_percent": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "Percentage of parameters to offload"}),
            },
        }
    RETURN_TYPES = ("VRAM_MANAGEMENTARGS",)
    RETURN_NAMES = ("vram_management_args",)
    FUNCTION = "setargs"
    CATEGORY = "WanVideoWrapper"
    DESCRIPTION = "Alternative offloading method from DiffSynth-Studio, more aggressive in reducing memory use than block swapping, but can be slower"

    def setargs(self, **kwargs):
        return (kwargs, )

class WanVideoTeaCache:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "rel_l1_thresh": ("FLOAT", {"default": 0.3, "min": 0.0, "max": 1.0, "step": 0.001,
                                            "tooltip": "Higher values will make TeaCache more aggressive, faster, but may cause artifacts. Good value range for 1.3B: 0.05 - 0.08, for other models 0.15-0.30"}),
                "start_step": ("INT", {"default": 1, "min": 0, "max": 9999, "step": 1, "tooltip": "Start percentage of the steps to apply TeaCache"}),
                "end_step": ("INT", {"default": -1, "min": -1, "max": 9999, "step": 1, "tooltip": "End steps to apply TeaCache"}),
                "cache_device": (["main_device", "offload_device"], {"default": "offload_device", "tooltip": "Device to cache to"}),
                "use_coefficients": ("BOOLEAN", {"default": True, "tooltip": "Use calculated coefficients for more accuracy. When enabled therel_l1_thresh should be about 10 times higher than without"}),
            },
        }
    RETURN_TYPES = ("TEACACHEARGS",)
    RETURN_NAMES = ("teacache_args",)
    FUNCTION = "process"
    CATEGORY = "WanVideoWrapper"
    DESCRIPTION = """
Patch WanVideo model to use TeaCache. Speeds up inference by caching the output and  
applying it instead of doing the step.  Best results are achieved by choosing the  
appropriate coefficients for the model. Early steps should never be skipped, with too  
aggressive values this can happen and the motion suffers. Starting later can help with that too.   
When NOT using coefficients, the threshold value should be  
about 10 times smaller than the value used with coefficients.  

Official recommended values https://github.com/ali-vilab/TeaCache/tree/main/TeaCache4Wan2.1:


<pre style='font-family:monospace'>
+-------------------+--------+---------+--------+
|       Model       |  Low   | Medium  |  High  |
+-------------------+--------+---------+--------+
| Wan2.1 t2v 1.3B  |  0.05  |  0.07   |  0.08  |
| Wan2.1 t2v 14B   |  0.14  |  0.15   |  0.20  |
| Wan2.1 i2v 480P  |  0.13  |  0.19   |  0.26  |
| Wan2.1 i2v 720P  |  0.18  |  0.20   |  0.30  |
+-------------------+--------+---------+--------+
</pre> 
"""
    EXPERIMENTAL = True

    def process(self, rel_l1_thresh, start_step, end_step, cache_device, use_coefficients):
        if cache_device == "main_device":
            teacache_device = mm.get_torch_device()
        else:
            teacache_device = mm.unet_offload_device()
        teacache_args = {
            "rel_l1_thresh": rel_l1_thresh,
            "start_step": start_step,
            "end_step": end_step,
            "cache_device": teacache_device,
            "use_coefficients": use_coefficients,
        }
        return (teacache_args,)


class WanVideoModel(comfy.model_base.BaseModel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pipeline = {}

    def __getitem__(self, k):
        return self.pipeline[k]

    def __setitem__(self, k, v):
        self.pipeline[k] = v

try:
    from comfy.latent_formats import Wan21
    latent_format = Wan21
except: #for backwards compatibility
    log.warning("Wan21 latent format not found, update ComfyUI for better livepreview")
    from comfy.latent_formats import HunyuanVideo
    latent_format = HunyuanVideo

class WanVideoModelConfig:
    def __init__(self, dtype):
        self.unet_config = {}
        self.unet_extra_config = {}
        self.latent_format = latent_format
        self.latent_format.latent_channels = 16
        self.manual_cast_dtype = dtype
        self.sampling_settings = {"multiplier": 1.0}
        # Don't know what this is. Value taken from ComfyUI Mochi model.
        self.memory_usage_factor = 2.0
        # denoiser is handled by extension
        self.unet_config["disable_unet_model_creation"] = True

def filter_state_dict_by_blocks(state_dict, blocks_mapping):
    filtered_dict = {}

    for key in state_dict:
        if 'blocks.' in key:
            block_pattern = key.split('diffusion_model.')[1].split('.', 2)[0:2]
            block_key = f'{block_pattern[0]}.{block_pattern[1]}.'

            if block_key in blocks_mapping:
                filtered_dict[key] = state_dict[key]

    return filtered_dict

def standardize_lora_key_format(lora_sd):
    new_sd = {}
    for k, v in lora_sd.items():
        # Diffusers format
        if k.startswith('transformer.'):
            k = k.replace('transformer.', 'diffusion_model.')
            
        # from finetrainer format
        if '.attn1.' in k:
            k = k.replace('.attn1.', '.cross_attn.')
            k = k.replace('.to_k.', '.k.')
            k = k.replace('.to_q.', '.q.')
            k = k.replace('.to_v.', '.v.')
            k = k.replace('.to_out.0.', '.o.')
        elif '.attn2.' in k:
            k = k.replace('.attn2.', '.cross_attn.')
            k = k.replace('.to_k.', '.k.')
            k = k.replace('.to_q.', '.q.')
            k = k.replace('.to_v.', '.v.')
            k = k.replace('.to_out.0.', '.o.')
            
        if "img_attn.proj" in k:
            k = k.replace("img_attn.proj", "img_attn_proj")
        if "img_attn.qkv" in k:
            k = k.replace("img_attn.qkv", "img_attn_qkv")
        if "txt_attn.proj" in k:
            k = k.replace("txt_attn.proj ", "txt_attn_proj")
        if "txt_attn.qkv" in k:
            k = k.replace("txt_attn.qkv", "txt_attn_qkv")
        new_sd[k] = v
    return new_sd

class WanVideoEnhanceAVideo:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "weight": ("FLOAT", {"default": 2.0, "min": 0, "max": 100, "step": 0.01, "tooltip": "The feta Weight of the Enhance-A-Video"}),
                "start_percent": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "Start percentage of the steps to apply Enhance-A-Video"}),
                "end_percent": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "End percentage of the steps to apply Enhance-A-Video"}),
            },
        }
    RETURN_TYPES = ("FETAARGS",)
    RETURN_NAMES = ("feta_args",)
    FUNCTION = "setargs"
    CATEGORY = "WanVideoWrapper"
    DESCRIPTION = "https://github.com/NUS-HPC-AI-Lab/Enhance-A-Video"

    def setargs(self, **kwargs):
        return (kwargs, )

class WanVideoLoraSelect:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
               "lora": (folder_paths.get_filename_list("loras"),
                {"tooltip": "LORA models are expected to be in ComfyUI/models/loras with .safetensors extension"}),
                "strength": ("FLOAT", {"default": 1.0, "min": -10.0, "max": 10.0, "step": 0.0001, "tooltip": "LORA strength, set to 0.0 to unmerge the LORA"}),
            },
            "optional": {
                "prev_lora":("WANVIDLORA", {"default": None, "tooltip": "For loading multiple LoRAs"}),
                "blocks":("SELECTEDBLOCKS", ),
                "low_mem_load": ("BOOLEAN", {"default": False, "tooltip": "Load the LORA model with less VRAM usage, slower loading"}),
            }
        }

    RETURN_TYPES = ("WANVIDLORA",)
    RETURN_NAMES = ("lora", )
    FUNCTION = "getlorapath"
    CATEGORY = "WanVideoWrapper"
    DESCRIPTION = "Select a LoRA model from ComfyUI/models/loras"

    def getlorapath(self, lora, strength, blocks=None, prev_lora=None, low_mem_load=False):
        loras_list = []

        lora = {
            "path": folder_paths.get_full_path("loras", lora),
            "strength": strength,
            "name": lora.split(".")[0],
            "blocks": blocks,
            "low_mem_load": low_mem_load,
        }
        if prev_lora is not None:
            loras_list.extend(prev_lora)

        loras_list.append(lora)
        return (loras_list,)

class WanVideoLoraBlockEdit:
    def __init__(self):
        self.loaded_lora = None

    @classmethod
    def INPUT_TYPES(s):
        arg_dict = {}
        argument = ("BOOLEAN", {"default": True})

        for i in range(40):
            arg_dict["blocks.{}.".format(i)] = argument

        return {"required": arg_dict}

    RETURN_TYPES = ("SELECTEDBLOCKS", )
    RETURN_NAMES = ("blocks", )
    OUTPUT_TOOLTIPS = ("The modified lora model",)
    FUNCTION = "select"

    CATEGORY = "WanVideoWrapper"

    def select(self, **kwargs):
        selected_blocks = {k: v for k, v in kwargs.items() if v is True and isinstance(v, bool)}
        print("Selected blocks LoRA: ", selected_blocks)
        return (selected_blocks,)

#region Model loading
class WanVideoModelLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": (folder_paths.get_filename_list("diffusion_models"), {"tooltip": "These models are loaded from the 'ComfyUI/models/diffusion_models' -folder",}),

            "base_precision": (["fp32", "bf16", "fp16", "fp16_fast"], {"default": "bf16"}),
            "quantization": (['disabled', 'fp8_e4m3fn', 'fp8_e4m3fn_fast', 'fp8_e5m2', 'torchao_fp8dq', "torchao_fp8dqrow", "torchao_int8dq", "torchao_fp6", "torchao_int4", "torchao_int8"], {"default": 'disabled', "tooltip": "optional quantization method"}),
            "load_device": (["main_device", "offload_device"], {"default": "main_device", "tooltip": "Initial device to load the model to, NOT recommended with the larger models unless you have 48GB+ VRAM"}),
            },
            "optional": {
                "attention_mode": ([
                    "sdpa",
                    "flash_attn_2",
                    "flash_attn_3",
                    "sageattn",
                    #"spargeattn", needs tuning
                    #"spargeattn_tune",
                    ], {"default": "sdpa"}),
                "compile_args": ("WANCOMPILEARGS", ),
                "block_swap_args": ("BLOCKSWAPARGS", ),
                "lora": ("WANVIDLORA", {"default": None}),
                "vram_management_args": ("VRAM_MANAGEMENTARGS", {"default": None, "tooltip": "Alternative offloading method from DiffSynth-Studio, more aggressive in reducing memory use than block swapping, but can be slower"}),
            }
        }

    RETURN_TYPES = ("WANVIDEOMODEL",)
    RETURN_NAMES = ("model", )
    FUNCTION = "loadmodel"
    CATEGORY = "WanVideoWrapper"

    def loadmodel(self, model, base_precision, load_device,  quantization,
                  compile_args=None, attention_mode="sdpa", block_swap_args=None, lora=None, vram_management_args=None):
        assert not (vram_management_args is not None and block_swap_args is not None), "Can't use both block_swap_args and vram_management_args at the same time"
        lora_low_mem_load = False
        if lora is not None:
            for l in lora:
                lora_low_mem_load = l.get("low_mem_load") if lora is not None else False

        transformer = None
        mm.unload_all_models()
        mm.cleanup_models()
        mm.soft_empty_cache()
        manual_offloading = True
        if "sage" in attention_mode:
            try:
                from sageattention import sageattn
            except Exception as e:
                raise ValueError(f"Can't import SageAttention: {str(e)}")

        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

                
        manual_offloading = True
        transformer_load_device = device if load_device == "main_device" else offload_device
        
        base_dtype = {"fp8_e4m3fn": torch.float8_e4m3fn, "fp8_e4m3fn_fast": torch.float8_e4m3fn, "bf16": torch.bfloat16, "fp16": torch.float16, "fp16_fast": torch.float16, "fp32": torch.float32}[base_precision]
        
        if base_precision == "fp16_fast":
            if hasattr(torch.backends.cuda.matmul, "allow_fp16_accumulation"):
                torch.backends.cuda.matmul.allow_fp16_accumulation = True
            else:
                raise ValueError("torch.backends.cuda.matmul.allow_fp16_accumulation is not available in this version of torch, requires torch 2.7.0.dev2025 02 26 nightly minimum currently")
        else:
            try:
                if hasattr(torch.backends.cuda.matmul, "allow_fp16_accumulation"):
                    torch.backends.cuda.matmul.allow_fp16_accumulation = False
            except:
                pass

        model_path = folder_paths.get_full_path_or_raise("diffusion_models", model)
        sd = load_torch_file(model_path, device=transformer_load_device, safe_load=True)

        first_key = next(iter(sd))
        if first_key.startswith("model.diffusion_model."):
            new_sd = {}
            for key, value in sd.items():
                new_key = key.replace("model.diffusion_model.", "", 1)
                new_sd[new_key] = value
            sd = new_sd

        dim = sd["patch_embedding.weight"].shape[0]
        in_channels = sd["patch_embedding.weight"].shape[1]
        print("in_channels: ", in_channels)
        ffn_dim = sd["blocks.0.ffn.0.bias"].shape[0]
        model_type = "i2v" if in_channels == 36 else "t2v"
        num_heads = 40 if dim == 5120 else 12
        num_layers = 40 if dim == 5120 else 30

        log.info(f"Model type: {model_type}, num_heads: {num_heads}, num_layers: {num_layers}")

        teacache_coefficients_map = {
            "1_3B": [2.39676752e+03, -1.31110545e+03, 2.01331979e+02, -8.29855975e+00, 1.37887774e-01],
            "14B": [-5784.54975374, 5449.50911966, -1811.16591783, 256.27178429, -13.02252404],
            "i2v_480": [-3.02331670e+02, 2.23948934e+02, -5.25463970e+01, 5.87348440e+00, -2.01973289e-01],
            "i2v_720": [-114.36346466, 65.26524496, -18.82220707, 4.91518089, -0.23412683],
        }
        if model_type == "i2v":
            model_variant = "i2v_480" if "480" in model else "i2v_720"
        elif model_type == "t2v":
            model_variant = "14B" if dim == 5120 else "1_3B"
        log.info(f"Model variant detected: {model_variant}")
        
        TRANSFORMER_CONFIG= {
            "dim": dim,
            "ffn_dim": ffn_dim,
            "eps": 1e-06,
            "freq_dim": 256,
            "in_dim": in_channels,
            "model_type": model_type,
            "out_dim": 16,
            "text_len": 512,
            "num_heads": num_heads,
            "num_layers": num_layers,
            "attention_mode": attention_mode,
            "main_device": device,
            "offload_device": offload_device,
            "teacache_coefficients": teacache_coefficients_map[model_variant],
        }

        with init_empty_weights():
            transformer = WanModel(**TRANSFORMER_CONFIG)
        transformer.eval()

        comfy_model = WanVideoModel(
            WanVideoModelConfig(base_dtype),
            model_type=comfy.model_base.ModelType.FLOW,
            device=device,
        )
          

        if not "torchao" in quantization:
            if quantization == "fp8_e4m3fn" or quantization == "fp8_e4m3fn_fast" or quantization == "fp8_scaled":
                dtype = torch.float8_e4m3fn
            elif quantization == "fp8_e5m2":
                dtype = torch.float8_e5m2
            else:
                dtype = base_dtype
            params_to_keep = {"norm", "head", "bias", "time_in", "vector_in", "patch_embedding", "time_", "img_emb", "modulation"}
            #if lora is not None:
            #    transformer_load_device = device
            if not lora_low_mem_load:
                log.info("Using accelerate to load and assign model weights to device...")
                param_count = sum(1 for _ in transformer.named_parameters())
                for name, param in tqdm(transformer.named_parameters(), 
                       desc=f"Loading transformer parameters to {transformer_load_device}", 
                       total=param_count,
                       leave=True):
                    dtype_to_use = base_dtype if any(keyword in name for keyword in params_to_keep) else dtype
                    set_module_tensor_to_device(transformer, name, device=transformer_load_device, dtype=dtype_to_use, value=sd[name])

            comfy_model.diffusion_model = transformer
            comfy_model.load_device = transformer_load_device
            
            patcher = comfy.model_patcher.ModelPatcher(comfy_model, device, offload_device)
            patcher.model.is_patched = False
            
            if lora is not None:
                for l in lora:
                    log.info(f"Loading LoRA: {l['name']} with strength: {l['strength']}")
                    lora_path = l["path"]
                    lora_strength = l["strength"]
                    lora_sd = load_torch_file(lora_path, safe_load=True)
                    lora_sd = standardize_lora_key_format(lora_sd)
                    if l["blocks"]:
                        lora_sd = filter_state_dict_by_blocks(lora_sd, l["blocks"])

                    #spacepxl's control LoRA patch
                    # for key in lora_sd.keys():
                    #     print(key)
                    if "diffusion_model.patch_embedding.lora_A.weight" in lora_sd:
                        log.info("Control-LoRA detected, patching model...")

                        in_cls = transformer.patch_embedding.__class__ # nn.Conv3d
                        old_in_dim = transformer.in_dim # 16
                        new_in_dim = lora_sd["diffusion_model.patch_embedding.lora_A.weight"].shape[1]
                        assert new_in_dim == 32
                        
                        new_in = in_cls(
                            new_in_dim,
                            transformer.patch_embedding.out_channels,
                            transformer.patch_embedding.kernel_size,
                            transformer.patch_embedding.stride,
                            transformer.patch_embedding.padding,
                        ).to(device=device, dtype=torch.bfloat16)
                        
                        new_in.weight.zero_()
                        new_in.bias.zero_()
                        
                        new_in.weight[:, :old_in_dim].copy_(transformer.patch_embedding.weight)
                        new_in.bias.copy_(transformer.patch_embedding.bias)
                        
                        transformer.patch_embedding = new_in
                        transformer.expanded_patch_embedding = new_in
                        transformer.register_to_config(in_dim=new_in_dim)

                    patcher, _ = load_lora_for_models(patcher, None, lora_sd, lora_strength, 0)
                    
                    del lora_sd
                
                patcher = apply_lora(patcher, device, transformer_load_device, params_to_keep=params_to_keep, dtype=dtype, base_dtype=base_dtype, state_dict=sd, low_mem_load=lora_low_mem_load)
                #patcher.load(device, full_load=True)
                patcher.model.is_patched = True

            del sd
            
            if quantization == "fp8_e4m3fn_fast":
                from .fp8_optimization import convert_fp8_linear
                #params_to_keep.update({"ffn"})
                print(params_to_keep)
                convert_fp8_linear(patcher.model.diffusion_model, base_dtype, params_to_keep=params_to_keep)

            if vram_management_args is not None:
                from .diffsynth.vram_management import enable_vram_management, AutoWrappedModule, AutoWrappedLinear
                from .wanvideo.modules.model import WanLayerNorm, WanRMSNorm

                total_params_in_model = sum(p.numel() for p in patcher.model.diffusion_model.parameters())
                log.info(f"Total number of parameters in the loaded model: {total_params_in_model}")

                offload_percent = vram_management_args["offload_percent"]
                offload_params = int(total_params_in_model * offload_percent)
                params_to_keep = total_params_in_model - offload_params
                log.info(f"Selected params to offload: {offload_params}")
            
                enable_vram_management(
                    patcher.model.diffusion_model,
                    module_map = {
                        torch.nn.Linear: AutoWrappedLinear,
                        torch.nn.Conv3d: AutoWrappedModule,
                        torch.nn.LayerNorm: AutoWrappedModule,
                        WanLayerNorm: AutoWrappedModule,
                        WanRMSNorm: AutoWrappedModule,
                    },
                    module_config = dict(
                        offload_dtype=dtype,
                        offload_device=offload_device,
                        onload_dtype=dtype,
                        onload_device=device,
                        computation_dtype=base_dtype,
                        computation_device=device,
                    ),
                    max_num_param=params_to_keep,
                    overflow_module_config = dict(
                        offload_dtype=dtype,
                        offload_device=offload_device,
                        onload_dtype=dtype,
                        onload_device=offload_device,
                        computation_dtype=base_dtype,
                        computation_device=device,
                    ),
                    compile_args = compile_args,
                )

            #compile
            if compile_args is not None and vram_management_args is None:
                torch._dynamo.config.cache_size_limit = compile_args["dynamo_cache_size_limit"]
                if compile_args["compile_transformer_blocks_only"]:
                    for i, block in enumerate(patcher.model.diffusion_model.blocks):
                        patcher.model.diffusion_model.blocks[i] = torch.compile(block, fullgraph=compile_args["fullgraph"], dynamic=compile_args["dynamic"], backend=compile_args["backend"], mode=compile_args["mode"])
                else:
                    patcher.model.diffusion_model = torch.compile(patcher.model.diffusion_model, fullgraph=compile_args["fullgraph"], dynamic=compile_args["dynamic"], backend=compile_args["backend"], mode=compile_args["mode"])        
            
            if load_device == "offload_device" and patcher.model.diffusion_model.device != offload_device:
                log.info(f"Moving diffusion model from {patcher.model.diffusion_model.device} to {offload_device}")
                patcher.model.diffusion_model.to(offload_device)
                gc.collect()
                mm.soft_empty_cache()

        elif "torchao" in quantization:
            try:
                from torchao.quantization import (
                quantize_,
                fpx_weight_only,
                float8_dynamic_activation_float8_weight,
                int8_dynamic_activation_int8_weight,
                int8_weight_only,
                int4_weight_only
            )
            except:
                raise ImportError("torchao is not installed")

            # def filter_fn(module: nn.Module, fqn: str) -> bool:
            #     target_submodules = {'attn1', 'ff'} # avoid norm layers, 1.5 at least won't work with quantized norm1 #todo: test other models
            #     if any(sub in fqn for sub in target_submodules):
            #         return isinstance(module, nn.Linear)
            #     return False

            if "fp6" in quantization:
                quant_func = fpx_weight_only(3, 2)
            elif "int4" in quantization:
                quant_func = int4_weight_only()
            elif "int8" in quantization:
                quant_func = int8_weight_only()
            elif "fp8dq" in quantization:
                quant_func = float8_dynamic_activation_float8_weight()
            elif 'fp8dqrow' in quantization:
                from torchao.quantization.quant_api import PerRow
                quant_func = float8_dynamic_activation_float8_weight(granularity=PerRow())
            elif 'int8dq' in quantization:
                quant_func = int8_dynamic_activation_int8_weight()

            log.info(f"Quantizing model with {quant_func}")
            comfy_model.diffusion_model = transformer
            patcher = comfy.model_patcher.ModelPatcher(comfy_model, device, offload_device)

            for i, block in enumerate(patcher.model.diffusion_model.blocks):
                log.info(f"Quantizing block {i}")
                for name, _ in block.named_parameters(prefix=f"blocks.{i}"):
                    #print(f"Parameter name: {name}")
                    set_module_tensor_to_device(patcher.model.diffusion_model, name, device=transformer_load_device, dtype=base_dtype, value=sd[name])
                if compile_args is not None:
                    patcher.model.diffusion_model.blocks[i] = torch.compile(block, fullgraph=compile_args["fullgraph"], dynamic=compile_args["dynamic"], backend=compile_args["backend"], mode=compile_args["mode"])
                quantize_(block, quant_func)
                print(block)
                #block.to(offload_device)
            for name, param in patcher.model.diffusion_model.named_parameters():
                if "blocks" not in name:
                    set_module_tensor_to_device(patcher.model.diffusion_model, name, device=transformer_load_device, dtype=base_dtype, value=sd[name])

            manual_offloading = False # to disable manual .to(device) calls
            log.info(f"Quantized transformer blocks to {quantization}")
            for name, param in patcher.model.diffusion_model.named_parameters():
                print(name, param.dtype)
                #param.data = param.data.to(self.vae_dtype).to(device)

            del sd
            mm.soft_empty_cache()

        patcher.model["dtype"] = base_dtype
        patcher.model["base_path"] = model_path
        patcher.model["model_name"] = model
        patcher.model["manual_offloading"] = manual_offloading
        patcher.model["quantization"] = "disabled"
        patcher.model["block_swap_args"] = block_swap_args
        patcher.model["auto_cpu_offload"] = True if vram_management_args is not None else False

        for model in mm.current_loaded_models:
            if model._model() == patcher:
                mm.current_loaded_models.remove(model)            

        return (patcher,)

#region load VAE

class WanVideoVAELoader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model_name": (folder_paths.get_filename_list("vae"), {"tooltip": "These models are loaded from 'ComfyUI/models/vae'"}),
            },
            "optional": {
                "precision": (["fp16", "fp32", "bf16"],
                    {"default": "bf16"}
                ),
            }
        }

    RETURN_TYPES = ("WANVAE",)
    RETURN_NAMES = ("vae", )
    FUNCTION = "loadmodel"
    CATEGORY = "WanVideoWrapper"
    DESCRIPTION = "Loads Wan VAE model from 'ComfyUI/models/vae'"

    def loadmodel(self, model_name, precision):
        from .wanvideo.wan_video_vae import WanVideoVAE

        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[precision]
        #with open(os.path.join(script_directory, 'configs', 'hy_vae_config.json')) as f:
        #    vae_config = json.load(f)
        model_path = folder_paths.get_full_path("vae", model_name)
        vae_sd = load_torch_file(model_path, safe_load=True)

        has_model_prefix = any(k.startswith("model.") for k in vae_sd.keys())
        if not has_model_prefix:
            vae_sd = {f"model.{k}": v for k, v in vae_sd.items()}
        
        vae = WanVideoVAE(dtype=dtype)
        vae.load_state_dict(vae_sd)
        vae.eval()
        vae.to(device = offload_device, dtype = dtype)
            

        return (vae,)

class WanVideoTinyVAELoader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model_name": (folder_paths.get_filename_list("vae_approx"), {"tooltip": "These models are loaded from 'ComfyUI/models/vae_approx'"}),
            },
            "optional": {
                "precision": (["fp16", "fp32", "bf16"],
                    {"default": "fp16"}
                ),
            }
        }

    RETURN_TYPES = ("WANVAE",)
    RETURN_NAMES = ("vae", )
    FUNCTION = "loadmodel"
    CATEGORY = "WanVideoWrapper"
    DESCRIPTION = "Loads Wan VAE model from 'ComfyUI/models/vae'"

    def loadmodel(self, model_name, precision):
        from .taehv import TAEHV

        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[precision]
        model_path = folder_paths.get_full_path("vae_approx", model_name)
        vae_sd = load_torch_file(model_path, safe_load=True)
        
        vae = TAEHV(vae_sd)
       
        vae.to(device = offload_device, dtype = dtype)

        return (vae,)



class WanVideoTorchCompileSettings:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "backend": (["inductor","cudagraphs"], {"default": "inductor"}),
                "fullgraph": ("BOOLEAN", {"default": False, "tooltip": "Enable full graph mode"}),
                "mode": (["default", "max-autotune", "max-autotune-no-cudagraphs", "reduce-overhead"], {"default": "default"}),
                "dynamic": ("BOOLEAN", {"default": False, "tooltip": "Enable dynamic mode"}),
                "dynamo_cache_size_limit": ("INT", {"default": 64, "min": 0, "max": 1024, "step": 1, "tooltip": "torch._dynamo.config.cache_size_limit"}),
                "compile_transformer_blocks_only": ("BOOLEAN", {"default": True, "tooltip": "Compile only the transformer blocks, usually enough and can make compilation faster and less error prone"}),

            },
        }
    RETURN_TYPES = ("WANCOMPILEARGS",)
    RETURN_NAMES = ("torch_compile_args",)
    FUNCTION = "set_args"
    CATEGORY = "WanVideoWrapper"
    DESCRIPTION = "torch.compile settings, when connected to the model loader, torch.compile of the selected layers is attempted. Requires Triton and torch 2.5.0 is recommended"

    def set_args(self, backend, fullgraph, mode, dynamic, dynamo_cache_size_limit, compile_transformer_blocks_only):

        compile_args = {
            "backend": backend,
            "fullgraph": fullgraph,
            "mode": mode,
            "dynamic": dynamic,
            "dynamo_cache_size_limit": dynamo_cache_size_limit,
            "compile_transformer_blocks_only": compile_transformer_blocks_only,
        }

        return (compile_args, )

#region TextEncode

class LoadWanVideoT5TextEncoder:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model_name": (folder_paths.get_filename_list("text_encoders"), {"tooltip": "These models are loaded from 'ComfyUI/models/text_encoders'"}),
                "precision": (["fp16", "fp32", "bf16"],
                    {"default": "bf16"}
                ),
            },
            "optional": {
                "load_device": (["main_device", "offload_device"], {"default": "offload_device"}),
                "quantization": (['disabled', 'fp8_e4m3fn'], {"default": 'disabled', "tooltip": "optional quantization method"}),
            }
        }

    RETURN_TYPES = ("WANTEXTENCODER",)
    RETURN_NAMES = ("wan_t5_model", )
    FUNCTION = "loadmodel"
    CATEGORY = "WanVideoWrapper"
    DESCRIPTION = "Loads Wan text_encoder model from 'ComfyUI/models/LLM'"

    def loadmodel(self, model_name, precision, load_device="offload_device", quantization="disabled"):
       
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        text_encoder_load_device = device if load_device == "main_device" else offload_device

        tokenizer_path = os.path.join(script_directory, "configs", "T5_tokenizer")

        dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[precision]

        model_path = folder_paths.get_full_path("text_encoders", model_name)
        sd = load_torch_file(model_path, safe_load=True)

        T5_text_encoder = T5EncoderModel(
            text_len=512,
            dtype=dtype,
            device=text_encoder_load_device,
            state_dict=sd,
            tokenizer_path=tokenizer_path,
            quantization=quantization
        )
        text_encoder = {
            "model": T5_text_encoder,
            "dtype": dtype,
        }
        
        return (text_encoder,)
    
class LoadWanVideoClipTextEncoder:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model_name": (folder_paths.get_filename_list("text_encoders"), {"tooltip": "These models are loaded from 'ComfyUI/models/text_encoders'"}),
                 "precision": (["fp16", "fp32", "bf16"],
                    {"default": "fp16"}
                ),
            },
            "optional": {
                "load_device": (["main_device", "offload_device"], {"default": "offload_device"}),
            }
        }

    RETURN_TYPES = ("CLIP_VISION",) 
    RETURN_NAMES = ("wan_clip_vision", )
    FUNCTION = "loadmodel"
    CATEGORY = "WanVideoWrapper"
    DESCRIPTION = "Loads Wan text_encoder model from 'ComfyUI/models/text_encoders'"

    def loadmodel(self, model_name, precision, load_device="offload_device"):
       
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        text_encoder_load_device = device if load_device == "main_device" else offload_device

        dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[precision]

        model_path = folder_paths.get_full_path("text_encoders", model_name)
        sd = load_torch_file(model_path, safe_load=True)
        clip_model = CLIPModel(dtype=dtype, device=device, state_dict=sd)
        clip_model.model.to(text_encoder_load_device)
        del sd
        
        return (clip_model,)
    

class WanVideoTextEncode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "t5": ("WANTEXTENCODER",),
            "positive_prompt": ("STRING", {"default": "", "multiline": True} ),
            "negative_prompt": ("STRING", {"default": "", "multiline": True} ),
            },
            "optional": {
                "force_offload": ("BOOLEAN", {"default": True}),
                "model_to_offload": ("WANVIDEOMODEL", {"tooltip": "Model to move to offload_device before encoding"}),
            }
        }

    RETURN_TYPES = ("WANVIDEOTEXTEMBEDS", )
    RETURN_NAMES = ("text_embeds",)
    FUNCTION = "process"
    CATEGORY = "WanVideoWrapper"
    DESCRIPTION = "Encodes text prompts into text embeddings. For context windowing you can input multiple prompts separated by '|'"

    def process(self, t5, positive_prompt, negative_prompt,force_offload=True, model_to_offload=None):

        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        if model_to_offload is not None:
            log.info(f"Moving video model to {offload_device}")
            model_to_offload.model.to(offload_device)
            mm.soft_empty_cache()

        encoder = t5["model"]
        dtype = t5["dtype"]

        # Split positive prompts and process each
        positive_prompts = [p.strip() for p in positive_prompt.split('|')]

        encoder.model.to(device)
       
        with torch.autocast(device_type=mm.get_autocast_device(device), dtype=dtype, enabled=True):
            context = encoder(positive_prompts, device)
            context_null = encoder([negative_prompt], device)


        context = [t.to(device) for t in context]
        context_null = [t.to(device) for t in context_null]

        if force_offload:
            encoder.model.to(offload_device)
            mm.soft_empty_cache()


        prompt_embeds_dict = {
                "prompt_embeds": context,
                "negative_prompt_embeds": context_null,
            }
        return (prompt_embeds_dict,)
    
class WanVideoTextEmbedBridge:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "positive": ("CONDITIONING",),
            "negative": ("CONDITIONING",),
            },
        }

    RETURN_TYPES = ("WANVIDEOTEXTEMBEDS", )
    RETURN_NAMES = ("text_embeds",)
    FUNCTION = "process"
    CATEGORY = "WanVideoWrapper"
    DESCRIPTION = "Bridge between ComfyUI native text embedding and WanVideoWrapper text embedding"

    def process(self, positive, negative):
        device=mm.get_torch_device()
        prompt_embeds_dict = {
                "prompt_embeds": positive[0][0].to(device),
                "negative_prompt_embeds": negative[0][0].to(device),
            }
        return (prompt_embeds_dict,)
    
#region clip image encode
class WanVideoImageClipEncode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "clip_vision": ("CLIP_VISION",),
            "image": ("IMAGE", {"tooltip": "Image to encode"}),
            "vae": ("WANVAE",),
            "generation_width": ("INT", {"default": 832, "min": 64, "max": 2048, "step": 8, "tooltip": "Width of the image to encode"}),
            "generation_height": ("INT", {"default": 480, "min": 64, "max": 29048, "step": 8, "tooltip": "Height of the image to encode"}),
            "num_frames": ("INT", {"default": 81, "min": 1, "max": 10000, "step": 4, "tooltip": "Number of frames to encode"}),
            },
            "optional": {
                "force_offload": ("BOOLEAN", {"default": True}),
                "noise_aug_strength": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 10.0, "step": 0.001, "tooltip": "Strength of noise augmentation, helpful for I2V where some noise can add motion and give sharper results"}),
                "latent_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.001, "tooltip": "Additional latent multiplier, helpful for I2V where lower values allow for more motion"}),
                "clip_embed_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.001, "tooltip": "Additional clip embed multiplier"}),
                "adjust_resolution": ("BOOLEAN", {"default": True, "tooltip": "Performs the same resolution adjustment as in the original code"}),

            }
        }

    RETURN_TYPES = ("WANVIDIMAGE_EMBEDS", )
    RETURN_NAMES = ("image_embeds",)
    FUNCTION = "process"
    CATEGORY = "WanVideoWrapper"

    def process(self, clip_vision, vae, image, num_frames, generation_width, generation_height, force_offload=True, noise_aug_strength=0.0, 
                latent_strength=1.0, clip_embed_strength=1.0, adjust_resolution=True):

        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        self.image_mean = [0.48145466, 0.4578275, 0.40821073]
        self.image_std = [0.26862954, 0.26130258, 0.27577711]
        patch_size = (1, 2, 2)
        vae_stride = (4, 8, 8)
    
        H, W = image.shape[1], image.shape[2]
        max_area = generation_width * generation_height

        
        print(clip_vision)
        clip_vision.model.to(device)
        if isinstance(clip_vision, ClipVisionModel):
            clip_context = clip_vision.encode_image(image).last_hidden_state.to(device)
        else:
            pixel_values = clip_preprocess(image.to(device), size=224, mean=self.image_mean, std=self.image_std, crop=True).float()
            clip_context = clip_vision.visual(pixel_values)
        if clip_embed_strength != 1.0:
            clip_context *= clip_embed_strength
        
        if force_offload:
            clip_vision.model.to(offload_device)
            mm.soft_empty_cache()

        if adjust_resolution:
            aspect_ratio = H / W
            lat_h = round(
            np.sqrt(max_area * aspect_ratio) // vae_stride[1] //
            patch_size[1] * patch_size[1])
            lat_w = round(
                np.sqrt(max_area / aspect_ratio) // vae_stride[2] //
                patch_size[2] * patch_size[2])
            h = lat_h * vae_stride[1]
            w = lat_w * vae_stride[2]
        else:
            h = generation_height
            w = generation_width
            lat_h = h // 8
            lat_w = w // 8

        # Step 1: Create initial mask with ones for first frame, zeros for others
        mask = torch.ones(1, num_frames, lat_h, lat_w, device=device)
        mask[:, 1:] = 0

        # Step 2: Repeat first frame 4 times and concatenate with remaining frames
        first_frame_repeated = torch.repeat_interleave(mask[:, 0:1], repeats=4, dim=1)
        mask = torch.concat([first_frame_repeated, mask[:, 1:]], dim=1)

        # Step 3: Reshape mask into groups of 4 frames
        mask = mask.view(1, mask.shape[1] // 4, 4, lat_h, lat_w)

        # Step 4: Transpose dimensions and select first batch
        mask = mask.transpose(1, 2)[0]

        # Calculate maximum sequence length
        frames_per_stride = (num_frames - 1) // vae_stride[0] + 1
        patches_per_frame = lat_h * lat_w // (patch_size[1] * patch_size[2])
        max_seq_len = frames_per_stride * patches_per_frame

        vae.to(device)

        # Step 1: Resize and rearrange the input image dimensions
        #resized_image = image.permute(0, 3, 1, 2)  # Rearrange dimensions to (B, C, H, W)
        #resized_image = torch.nn.functional.interpolate(resized_image, size=(h, w), mode='bicubic')
        resized_image = common_upscale(image.movedim(-1, 1), w, h, "lanczos", "disabled")
        resized_image = resized_image.transpose(0, 1)  # Transpose to match required format
        resized_image = resized_image * 2 - 1

        if noise_aug_strength > 0.0:
            resized_image = add_noise_to_reference_video(resized_image, ratio=noise_aug_strength)
        
        # Step 2: Create zero padding frames
        zero_frames = torch.zeros(3, num_frames-1, h, w, device=device)

        # Step 3: Concatenate image with zero frames
        concatenated = torch.concat([resized_image.to(device), zero_frames, resized_image.to(device)], dim=1).to(device = device, dtype = vae.dtype)
        concatenated *= latent_strength
        y = vae.encode([concatenated], device)[0]

        y = torch.concat([mask, y])

        vae.model.clear_cache()
        vae.to(offload_device)

        image_embeds = {
            "image_embeds": y,
            "clip_context": clip_context,
            "max_seq_len": max_seq_len,
            "num_frames": num_frames,
            "lat_h": lat_h,
            "lat_w": lat_w,
        }

        return (image_embeds,)
    
class WanVideoEmptyEmbeds:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "width": ("INT", {"default": 832, "min": 64, "max": 2048, "step": 8, "tooltip": "Width of the image to encode"}),
            "height": ("INT", {"default": 480, "min": 64, "max": 29048, "step": 8, "tooltip": "Height of the image to encode"}),
            "num_frames": ("INT", {"default": 81, "min": 1, "max": 10000, "step": 4, "tooltip": "Number of frames to encode"}),
            },
        }

    RETURN_TYPES = ("WANVIDIMAGE_EMBEDS", )
    RETURN_NAMES = ("image_embeds",)
    FUNCTION = "process"
    CATEGORY = "WanVideoWrapper"

    def process(self, num_frames, width, height):

        patch_size = (1, 2, 2)
        vae_stride = (4, 8, 8)

        target_shape = (16, (num_frames - 1) // vae_stride[0] + 1,
                        height // vae_stride[1],
                        width // vae_stride[2])

        seq_len = math.ceil((target_shape[2] * target_shape[3]) /
                            (patch_size[1] * patch_size[2]) *
                            target_shape[1])
        
        embeds = {
            "max_seq_len": seq_len,
            "target_shape": target_shape,
            "num_frames": num_frames
        }
    
        return (embeds,)
    
class WanVideoControlEmbeds:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "latents": ("LATENT", {"tooltip": "Encoded latents to use as control signals"}),
            "start_percent": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "Start percent of the control signal"}),
            "end_percent": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "End percent of the control signal"}),
            },
        }

    RETURN_TYPES = ("WANVIDIMAGE_EMBEDS", )
    RETURN_NAMES = ("image_embeds",)
    FUNCTION = "process"
    CATEGORY = "WanVideoWrapper"

    def process(self, latents, start_percent, end_percent):

        samples = latents["samples"].squeeze(0)
        C, T, H, W = samples.shape

        num_frames = (T - 1) * 4 + 1
        seq_len = math.ceil((H * W) / 4 * ((num_frames - 1) // 4 + 1))
      
        embeds = {
            "max_seq_len": seq_len,
            "target_shape": samples.shape,
            "num_frames": num_frames,
            "control_images": samples,
            "start_percent": start_percent,
            "end_percent": end_percent,
        }
    
        return (embeds,)
    
class WanVideoSLG:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "blocks": ("STRING", {"default": "10", "tooltip": "Blocks to skip uncond on, separated by comma, index starts from 0"}),
            "start_percent": ("FLOAT", {"default": 0.1, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "Start percent of the control signal"}),
            "end_percent": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "End percent of the control signal"}),
            },
        }

    RETURN_TYPES = ("SLGARGS", )
    RETURN_NAMES = ("slg_args",)
    FUNCTION = "process"
    CATEGORY = "WanVideoWrapper"
    DESCRIPTION = "Skips uncond on the selected blocks"

    def process(self, blocks, start_percent, end_percent):
        slg_block_list = [int(x.strip()) for x in blocks.split(",")]

        slg_args = {
            "blocks": slg_block_list,
            "start_percent": start_percent,
            "end_percent": end_percent,
        }
        return (slg_args,)


#region Sampler

class WanVideoContextOptions:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "context_schedule": (["uniform_standard", "uniform_looped", "static_standard"],),
            "context_frames": ("INT", {"default": 81, "min": 2, "max": 1000, "step": 1, "tooltip": "Number of pixel frames in the context, NOTE: the latent space has 4 frames in 1"} ),
            "context_stride": ("INT", {"default": 4, "min": 4, "max": 100, "step": 1, "tooltip": "Context stride as pixel frames, NOTE: the latent space has 4 frames in 1"} ),
            "context_overlap": ("INT", {"default": 16, "min": 4, "max": 100, "step": 1, "tooltip": "Context overlap as pixel frames, NOTE: the latent space has 4 frames in 1"} ),
            "freenoise": ("BOOLEAN", {"default": True, "tooltip": "Shuffle the noise"}),
            "verbose": ("BOOLEAN", {"default": False, "tooltip": "Print debug output"}),
            },
            "optional": {
                "image_cond_start_step": ("INT", {"default": 6, "min": 0, "max": 10000, "step": 1, "tooltip": "!EXPERIMENTAL! Start step of using previous window results as input instead of the init image"}),
                "image_cond_window_count": ("INT", {"default": 2, "min": 1, "max": 10000, "step": 1, "tooltip": "!EXPERIMENTAL! Number of image 'prompt windows'"}),
                "vae": ("WANVAE",),
            }
        }

    RETURN_TYPES = ("WANVIDCONTEXT", )
    RETURN_NAMES = ("context_options",)
    FUNCTION = "process"
    CATEGORY = "WanVideoWrapper"
    DESCRIPTION = "Context options for WanVideo, allows splitting the video into context windows and attemps blending them for longer generations than the model and memory otherwise would allow."

    def process(self, context_schedule, context_frames, context_stride, context_overlap, freenoise, verbose, image_cond_start_step=6, image_cond_window_count=2, vae=None):
        context_options = {
            "context_schedule":context_schedule,
            "context_frames":context_frames,
            "context_stride":context_stride,
            "context_overlap":context_overlap,
            "freenoise":freenoise,
            "verbose":verbose,
            "image_cond_start_step": image_cond_start_step,
            "image_cond_window_count": image_cond_window_count,
            "vae": vae,
        }

        return (context_options,)
    
class WanVideoFlowEdit:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                "source_embeds": ("WANVIDEOTEXTEMBEDS", ),
                "skip_steps": ("INT", {"default": 4, "min": 0}),
                "drift_steps": ("INT", {"default": 0, "min": 0}),
                "drift_flow_shift": ("FLOAT", {"default": 3.0, "min": 1.0, "max": 30.0, "step": 0.01}),
                "source_cfg": ("FLOAT", {"default": 6.0, "min": 0.0, "max": 30.0, "step": 0.01}),
                "drift_cfg": ("FLOAT", {"default": 6.0, "min": 0.0, "max": 30.0, "step": 0.01}),
            },
            "optional": {
                "source_image_embeds": ("WANVIDIMAGE_EMBEDS", ),
            }
        }

    RETURN_TYPES = ("FLOWEDITARGS", )
    RETURN_NAMES = ("flowedit_args",)
    FUNCTION = "process"
    CATEGORY = "WanVideoWrapper"
    DESCRIPTION = "Flowedit options for WanVideo"

    def process(self, **kwargs):
        return (kwargs,)
    
class WanVideoLoopArgs:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                "shift_skip": ("INT", {"default": 6, "min": 0, "tooltip": "Skip step of latent shift"}),
                "start_percent": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "Start percent of the looping effect"}),
                "end_percent": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "End percent of the looping effect"}),
            },
        }

    RETURN_TYPES = ("LOOPARGS", )
    RETURN_NAMES = ("loop_args",)
    FUNCTION = "process"
    CATEGORY = "WanVideoWrapper"
    DESCRIPTION = "Looping through latent shift as shown in https://github.com/YisuiTT/Mobius/"

    def process(self, **kwargs):
        return (kwargs,)
    
class WanVideoSampler:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("WANVIDEOMODEL",),
                "text_embeds": ("WANVIDEOTEXTEMBEDS", ),
                "image_embeds": ("WANVIDIMAGE_EMBEDS", ),
                "steps": ("INT", {"default": 30, "min": 1}),
                "cfg": ("FLOAT", {"default": 6.0, "min": 0.0, "max": 30.0, "step": 0.01}),
                "shift": ("FLOAT", {"default": 5.0, "min": 0.0, "max": 1000.0, "step": 0.01}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                "force_offload": ("BOOLEAN", {"default": True, "tooltip": "Moves the model to the offload device after sampling"}),
                "scheduler": (["unipc", "dpm++", "dpm++_sde", "euler"],
                    {
                        "default": 'unipc'
                    }),
                "riflex_freq_index": ("INT", {"default": 0, "min": 0, "max": 1000, "step": 1, "tooltip": "Frequency index for RIFLEX, disabled when 0, default 6. Allows for new frames to be generated after without looping"}),


            },
            "optional": {
                "samples": ("LATENT", {"tooltip": "init Latents to use for video2video process"} ),
                "denoise_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
                "feta_args": ("FETAARGS", ),
                "context_options": ("WANVIDCONTEXT", ),
                "teacache_args": ("TEACACHEARGS", ),
                "flowedit_args": ("FLOWEDITARGS", ),
                "batched_cfg": ("BOOLEAN", {"default": False, "tooltip": "Batc cond and uncond for faster sampling, possibly faster on some hardware, uses more memory"}),
                "slg_args": ("SLGARGS", ),
                "rope_function": (["default", "comfy"], {"default": "default", "tooltip": "!EXPERIMENTAL! Comfy's RoPE implementation doesn't use complex numbers and can thus be compiled, that should be a lot faster when using torch.compile"}),
                "loop_args": ("LOOPARGS", ),
            }
        }

    RETURN_TYPES = ("LATENT", )
    RETURN_NAMES = ("samples",)
    FUNCTION = "process"
    CATEGORY = "WanVideoWrapper"

    def process(self, model, text_embeds, image_embeds, shift, steps, cfg, seed, scheduler, riflex_freq_index, 
        force_offload=True, samples=None, feta_args=None, denoise_strength=1.0, context_options=None, 
        teacache_args=None, flowedit_args=None, batched_cfg=False, slg_args=None, rope_function="default", loop_args=None):
        #assert not (context_options and teacache_args), "Context options cannot currently be used together with teacache."
        patcher = model
        model = model.model
        transformer = model.diffusion_model

        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()
        
        steps = int(steps/denoise_strength)

        if scheduler == 'unipc':
            sample_scheduler = FlowUniPCMultistepScheduler(
                num_train_timesteps=1000,
                shift=shift,
                use_dynamic_shifting=False)
            sample_scheduler.set_timesteps(
                steps, device=device, shift=shift)
            timesteps = sample_scheduler.timesteps
        elif scheduler == 'euler':
            sample_scheduler = FlowMatchEulerDiscreteScheduler(
                num_train_timesteps=1000,
                shift=shift,
                use_dynamic_shifting=False)
            sampling_sigmas = get_sampling_sigmas(steps, shift)
            timesteps, _ = retrieve_timesteps(
                sample_scheduler,
                device=device,
                sigmas=sampling_sigmas)
        elif 'dpm++' in scheduler:
            if scheduler == 'dpm++_sde':
                algorithm_type = "sde-dpmsolver++"
            else:
                algorithm_type = "dpmsolver++"
            sample_scheduler = FlowDPMSolverMultistepScheduler(
                num_train_timesteps=1000,
                shift=shift,
                use_dynamic_shifting=False,
                algorithm_type= algorithm_type)
            sampling_sigmas = get_sampling_sigmas(steps, shift)
            timesteps, _ = retrieve_timesteps(
                sample_scheduler,
                device=device,
                sigmas=sampling_sigmas)
        else:
            raise NotImplementedError("Unsupported solver.")
        
        if denoise_strength < 1.0:
            steps = int(steps * denoise_strength)
            timesteps = timesteps[-(steps + 1):] 
        
        seed_g = torch.Generator(device=torch.device("cpu"))
        seed_g.manual_seed(seed)
        image_cond = None
        clip_fea = None
        control_latents = None
        if transformer.model_type == "i2v":
            lat_h = image_embeds.get("lat_h", None)
            lat_w = image_embeds.get("lat_w", None)
            if lat_h is None or lat_w is None:
                raise ValueError("Clip encoded image embeds must be provided for I2V (Image to Video) model")
            noise = torch.randn(
                16,
                (image_embeds["num_frames"] - 1) // 4 + 1,
                lat_h,
                lat_w,
                dtype=torch.float32,
                generator=seed_g,
                device=torch.device("cpu"))
            seq_len = image_embeds["max_seq_len"]
            image_cond = image_embeds.get("image_embeds", None)
            print("image_cond", image_cond.shape)
            clip_fea = image_embeds.get("clip_context", None)
            
        else: #t2v
            target_shape = image_embeds.get("target_shape", None)
            if target_shape is None:
                raise ValueError("Empty image embeds must be provided for T2V (Text to Video")
            seq_len = image_embeds["max_seq_len"]
            noise = torch.randn(
                    target_shape[0],
                    target_shape[1],
                    target_shape[2],
                    target_shape[3],
                    dtype=torch.float32,
                    device=torch.device("cpu"),
                    generator=seed_g)
            
            control_latents = image_embeds.get("control_images", None)
            if control_latents is not None:
                image_cond = control_latents.to(device)
                control_start_percent = image_embeds.get("start_percent", 0.0)
                control_end_percent = image_embeds.get("end_percent", 1.0)
                
                if not patcher.model.is_patched:
                    log.info("Re-loading control LoRA...")
                    patcher = apply_lora(patcher, device, device, low_mem_load=False)
                    patcher.model.is_patched = True
            
        latent_video_length = noise.shape[1]

        is_looped = False
        if context_options is not None:
            def create_window_mask(noise_pred_context, c, latent_video_length, context_overlap, looped=False):
                window_mask = torch.ones_like(noise_pred_context)
                
                # Apply left-side blending for all except first chunk (or always in loop mode)
                if min(c) > 0 or (looped and max(c) == latent_video_length - 1):
                    ramp_up = torch.linspace(0, 1, context_overlap, device=noise_pred_context.device)
                    ramp_up = ramp_up.view(1, -1, 1, 1)
                    window_mask[:, :context_overlap] = ramp_up
                    
                # Apply right-side blending for all except last chunk (or always in loop mode)
                if max(c) < latent_video_length - 1 or (looped and min(c) == 0):
                    ramp_down = torch.linspace(1, 0, context_overlap, device=noise_pred_context.device)
                    ramp_down = ramp_down.view(1, -1, 1, 1)
                    window_mask[:, -context_overlap:] = ramp_down
                    
                return window_mask
            
            context_schedule = context_options["context_schedule"]
            context_frames =  (context_options["context_frames"] - 1) // 4 + 1
            context_stride = context_options["context_stride"] // 4
            context_overlap = context_options["context_overlap"] // 4
            context_vae = context_options.get("vae", None)
            if context_vae is not None:
                context_vae.to(device)

            self.window_tracker = WindowTracker(verbose=context_options["verbose"])

            # Get total number of prompts
            num_prompts = len(text_embeds["prompt_embeds"])
            log.info(f"Number of prompts: {num_prompts}")
            # Calculate which section this context window belongs to
            section_size = latent_video_length / num_prompts
            log.info(f"Section size: {section_size}")
            is_looped = context_schedule == "uniform_looped"

            seq_len = math.ceil((noise.shape[2] * noise.shape[3]) / 4 * context_frames)

            if context_options["freenoise"]:
                log.info("Applying FreeNoise")
                # code from AnimateDiff-Evolved by Kosinkadink (https://github.com/Kosinkadink/ComfyUI-AnimateDiff-Evolved)
                delta = context_frames - context_overlap
                for start_idx in range(0, latent_video_length-context_frames, delta):
                    place_idx = start_idx + context_frames
                    if place_idx >= latent_video_length:
                        break
                    end_idx = place_idx - 1

                    if end_idx + delta >= latent_video_length:
                        final_delta = latent_video_length - place_idx
                        list_idx = torch.tensor(list(range(start_idx,start_idx+final_delta)), device=torch.device("cpu"), dtype=torch.long)
                        list_idx = list_idx[torch.randperm(final_delta, generator=seed_g)]
                        noise[:, place_idx:place_idx + final_delta, :, :] = noise[:, list_idx, :, :]
                        break
                    list_idx = torch.tensor(list(range(start_idx,start_idx+delta)), device=torch.device("cpu"), dtype=torch.long)
                    list_idx = list_idx[torch.randperm(delta, generator=seed_g)]
                    noise[:, place_idx:place_idx + delta, :, :] = noise[:, list_idx, :, :]
            
            log.info(f"Context schedule enabled: {context_frames} frames, {context_stride} stride, {context_overlap} overlap")
            from .context import get_context_scheduler
            context = get_context_scheduler(context_schedule)

        if samples is not None and denoise_strength < 1.0:
            latent_timestep = timesteps[:1].to(noise)
            noise = noise * latent_timestep / 1000 + (1 - latent_timestep / 1000) * samples["samples"].squeeze(0).to(noise)

        if samples is not None:
            original_image = samples["samples"].clone().squeeze(0).to(device)
            mask = samples.get("mask", None)

        latent = noise.to(device)

        freqs = None
        transformer.rope_embedder.k = None
        transformer.rope_embedder.num_frames = None
        if rope_function=="comfy":
            transformer.rope_embedder.k = riflex_freq_index
            transformer.rope_embedder.num_frames = latent_video_length
        else:
            d = transformer.dim // transformer.num_heads
            freqs = torch.cat([
                rope_params(1024, d - 4 * (d // 6), L_test=latent_video_length, k=riflex_freq_index),
                rope_params(1024, 2 * (d // 6)),
                rope_params(1024, 2 * (d // 6))
            ],
            dim=1)

        if not isinstance(cfg, list):
            cfg = [cfg] * (steps +1)

        print("Seq len:", seq_len)
           
        pbar = ProgressBar(steps)

        from .latent_preview import prepare_callback
        callback = prepare_callback(patcher, steps)

        #blockswap init
        if model["block_swap_args"] is not None:
            transformer.use_non_blocking = model["block_swap_args"].get("use_non_blocking", True)
            for name, param in transformer.named_parameters():
                if "block" not in name:
                    param.data = param.data.to(device)
                elif model["block_swap_args"]["offload_txt_emb"] and "txt_emb" in name:
                    param.data = param.data.to(offload_device, non_blocking=transformer.use_non_blocking)
                elif model["block_swap_args"]["offload_img_emb"] and "img_emb" in name:
                    param.data = param.data.to(offload_device, non_blocking=transformer.use_non_blocking)

            transformer.block_swap(
                model["block_swap_args"]["blocks_to_swap"] - 1 ,
                model["block_swap_args"]["offload_txt_emb"],
                model["block_swap_args"]["offload_img_emb"],
            )

        elif model["auto_cpu_offload"]:
            for module in transformer.modules():
                if hasattr(module, "offload"):
                    module.offload()
                if hasattr(module, "onload"):
                    module.onload()
        elif model["manual_offloading"]:
            transformer.to(device)
        #feta
        if feta_args is not None:
            set_enhance_weight(feta_args["weight"])
            feta_start_percent = feta_args["start_percent"]
            feta_end_percent = feta_args["end_percent"]
            if context_options is not None:
                set_num_frames(context_frames)
            else:
                set_num_frames(latent_video_length)
            enable_enhance()
        else:
            disable_enhance()

        # Initialize TeaCache if enabled
        if teacache_args is not None:
            transformer.enable_teacache = True
            transformer.rel_l1_thresh = teacache_args["rel_l1_thresh"]
            transformer.teacache_start_step = teacache_args["start_step"]
            transformer.teacache_cache_device = teacache_args["cache_device"]
            transformer.teacache_end_step = len(timesteps)-1 if teacache_args["end_step"] == -1 else teacache_args["end_step"]
            transformer.teacache_use_coefficients = teacache_args["use_coefficients"]
        else:
            transformer.enable_teacache = False

        if slg_args is not None:
            assert batched_cfg is not None, "Batched cfg is not supported with SLG"
            transformer.slg_blocks = slg_args["blocks"]
            transformer.slg_start_percent = slg_args["start_percent"]
            transformer.slg_end_percent = slg_args["end_percent"]
        else:
            transformer.slg_blocks = None

        mm.unload_all_models()
        mm.soft_empty_cache()
        gc.collect()

        self.teacache_state = [None, None]
        self.teacache_state_source = [None, None]
        self.teacache_states_context = []


        if "sparge" in transformer.attention_mode:
            from spas_sage_attn.autotune import (
                SparseAttentionMeansim,
                extract_sparse_attention_state_dict,
                load_sparse_attention_state_dict,
            )
                
            for idx, block in enumerate(transformer.blocks):
                block.self_attn.verbose = True
                block.self_attn.inner_attention = SparseAttentionMeansim(l1=0.06, pv_l1=0.065)
            if transformer.attention_mode == "spargeattn":
                try:
                    saved_state_dict = torch.load("sparge_wan.pt")
                except:
                    raise ValueError("No saved parameters found for sparse attention, tuning is required first")
                load_sparse_attention_state_dict(transformer, saved_state_dict, verbose = True)

        if flowedit_args is not None:
            source_embeds = flowedit_args["source_embeds"]
            source_image_embeds = flowedit_args.get("source_image_embeds", image_embeds)
            source_image_cond = source_image_embeds.get("image_embeds", None)
            source_clip_fea = source_image_embeds.get("clip_fea", None)
            skip_steps = flowedit_args["skip_steps"]
            drift_steps = flowedit_args["drift_steps"]
            source_cfg = flowedit_args["source_cfg"]
            if not isinstance(source_cfg, list):
                source_cfg = [source_cfg] * (steps +1)
            drift_cfg = flowedit_args["drift_cfg"]
            if not isinstance(drift_cfg, list):
                drift_cfg = [drift_cfg] * (steps +1)

            x_init = samples["samples"].clone().squeeze(0).to(device)
            x_tgt = samples["samples"].squeeze(0).to(device)

            sample_scheduler = FlowMatchEulerDiscreteScheduler(
                num_train_timesteps=1000,
                shift=flowedit_args["drift_flow_shift"],
                use_dynamic_shifting=False)

            sampling_sigmas = get_sampling_sigmas(steps, flowedit_args["drift_flow_shift"])
           
            drift_timesteps, _ = retrieve_timesteps(
                sample_scheduler,
                device=device,
                sigmas=sampling_sigmas)

            if drift_steps > 0:
                drift_timesteps = torch.cat([drift_timesteps, torch.tensor([0]).to(drift_timesteps.device)]).to(drift_timesteps.device)
                timesteps[-drift_steps:] = drift_timesteps[-drift_steps:]

        def predict_with_cfg(z, cfg_scale, positive_embeds, negative_embeds, timestep, idx, image_cond=None, clip_fea=None, teacache_state=None):
            with torch.autocast(device_type=mm.get_autocast_device(device), dtype=model["dtype"], enabled=True):
                nonlocal patcher
                current_step_percentage = idx / len(timesteps)
                control_enabled = False
                if control_latents is not None:
                    control_enabled = True
                    if not control_start_percent <= current_step_percentage <= control_end_percent:
                        image_cond = None
                        control_enabled = False
                        if patcher.model.is_patched:
                            log.info("Unloading LoRA...")
                            patcher.unpatch_model(device)
                            patcher.model.is_patched = False
                    else:
                        if not patcher.model.is_patched:
                            log.info("Loading LoRA...")
                            patcher = apply_lora(patcher, device, device, low_mem_load=False)
                            patcher.model.is_patched = True
    
                base_params = {
                    'clip_fea': clip_fea,
                    'seq_len': seq_len,
                    'device': device,
                    'freqs': freqs,
                    't': timestep,
                    'current_step': idx,
                    'y': [image_cond] if image_cond is not None else None,
                    'control_enabled': control_enabled,
                }
                
                if not batched_cfg:
                    #cond
                    noise_pred_cond, teacache_state_cond = transformer(
                        [z], context=[positive_embeds], is_uncond=False, current_step_percentage=current_step_percentage,
                        pred_id=teacache_state[0] if teacache_state else None,
                        **base_params
                    )
                    noise_pred_cond = noise_pred_cond[0].to(intermediate_device)
                    if math.isclose(cfg_scale, 1.0):
                        return noise_pred_cond, [teacache_state_cond]
                    #uncond
                    noise_pred_uncond, teacache_state_uncond = transformer(
                        [z], context=negative_embeds, is_uncond=True, current_step_percentage=current_step_percentage,
                        pred_id=teacache_state[1] if teacache_state else None,
                        **base_params
                    )
                    noise_pred_uncond=noise_pred_uncond[0].to(intermediate_device)
                    return noise_pred_uncond + cfg_scale * (noise_pred_cond - noise_pred_uncond), [teacache_state_cond, teacache_state_uncond]
                #batched
                else:
                    [noise_pred_cond, noise_pred_uncond], teacache_state_cond = transformer(
                        [z] + [z], context= [positive_embeds] + negative_embeds, is_uncond=False, current_step_percentage=current_step_percentage,
                        pred_id=teacache_state[0] if teacache_state else None,
                        **base_params
                    )
                    noise_pred_uncond=noise_pred_uncond.to(intermediate_device)

                
                return noise_pred_uncond + cfg_scale * (noise_pred_cond - noise_pred_uncond), [teacache_state_cond]
        
        try:
            torch.cuda.reset_peak_memory_stats(device)
        except:
            pass

        log.info(f"Sampling {(latent_video_length-1) * 4 + 1} frames at {latent.shape[3]*8}x{latent.shape[2]*8} with {steps} steps")

        intermediate_device = device

        # diff diff prep
        masks = None
        if samples is not None and mask is not None:
            mask = 1 - mask
            thresholds = torch.arange(len(timesteps), dtype=original_image.dtype) / len(timesteps)
            thresholds = thresholds.unsqueeze(1).unsqueeze(1).unsqueeze(1).unsqueeze(1).to(device)
            masks = mask.repeat(len(timesteps), 1, 1, 1, 1).to(device) 
            masks = masks > thresholds

        latent_shift_loop = False
        if loop_args is not None:
            latent_shift_loop = True
            is_looped = True
            latent_skip = loop_args["shift_skip"]
            latent_shift_start_percent = loop_args["start_percent"]
            latent_shift_end_percent = loop_args["end_percent"]
            shift_idx = 0
        #main loop start
        for idx, t in enumerate(tqdm(timesteps)):    
            if flowedit_args is not None:
                if idx < skip_steps:
                    continue

            # diff diff
            if masks is not None:
                if idx < len(timesteps) - 1:
                    noise_timestep = timesteps[idx+1]
                    image_latent = sample_scheduler.scale_noise(
                        original_image, torch.tensor([noise_timestep]), noise.to(device)
                    )
                    mask = masks[idx]
                    mask = mask.to(latent)
                    latent = image_latent * mask + latent * (1-mask)
                    # end diff diff

            latent_model_input = latent.to(device)

            timestep = torch.tensor([t]).to(device)
            current_step_percentage = idx / len(timesteps)

            ### latent shift
            if latent_shift_loop:
                if latent_shift_start_percent <= current_step_percentage <= latent_shift_end_percent:
                    latent_model_input = torch.cat([latent_model_input[:, shift_idx:]] + [latent_model_input[:, :shift_idx]], dim=1)

            #enhance-a-video
            if feta_args is not None:
                if feta_start_percent <= current_step_percentage <= feta_end_percent:
                    enable_enhance()
                else:
                    disable_enhance()
            #flow-edit
            if flowedit_args is not None:
                sigma = t / 1000.0
                sigma_prev = (timesteps[idx + 1] if idx < len(timesteps) - 1 else timesteps[-1]) / 1000.0
                noise = torch.randn(x_init.shape, generator=seed_g, device=torch.device("cpu"))
                if idx < len(timesteps) - drift_steps:
                    cfg = drift_cfg
                
                zt_src = (1-sigma) * x_init + sigma * noise.to(t)
                zt_tgt = x_tgt + zt_src - x_init

                #source
                if idx < len(timesteps) - drift_steps:
                    if context_options is not None:
                        counter = torch.zeros_like(zt_src, device=intermediate_device)
                        vt_src = torch.zeros_like(zt_src, device=intermediate_device)
                        context_queue = list(context(idx, steps, latent_video_length, context_frames, context_stride, context_overlap))
                        for c in context_queue:
                            window_id = self.window_tracker.get_window_id(c)

                            if teacache_args is not None:
                                current_teacache = self.window_tracker.get_teacache(window_id, self.teacache_state)
                            else:
                                current_teacache = None

                            prompt_index = min(int(max(c) / section_size), num_prompts - 1)
                            if context_options["verbose"]:
                                log.info(f"Prompt index: {prompt_index}")

                            positive = source_embeds["prompt_embeds"][prompt_index]

                            partial_img_emb = None
                            if source_image_cond is not None:
                                partial_img_emb = source_image_cond[:, c, :, :]
                                partial_img_emb[:, 0, :, :] = source_image_cond[:, 0, :, :].to(intermediate_device)

                            partial_zt_src = zt_src[:, c, :, :]
                            vt_src_context, new_teacache = predict_with_cfg(
                                partial_zt_src, cfg[idx], 
                                positive, source_embeds["negative_prompt_embeds"],
                                timestep, idx, partial_img_emb, 
                                source_clip_fea, current_teacache)
                            
                            if teacache_args is not None:
                                self.window_tracker.teacache_states[window_id] = new_teacache

                            window_mask = create_window_mask(vt_src_context, c, latent_video_length, context_overlap)
                            vt_src[:, c, :, :] += vt_src_context * window_mask
                            counter[:, c, :, :] += window_mask
                        vt_src /= counter
                    else:
                        vt_src, self.teacache_state_source = predict_with_cfg(
                            zt_src, cfg[idx], 
                            source_embeds["prompt_embeds"][0], 
                            source_embeds["negative_prompt_embeds"],
                            timestep, idx, source_image_cond, 
                            source_clip_fea,
                            teacache_state=self.teacache_state_source)
                else:
                    if idx == len(timesteps) - drift_steps:
                        x_tgt = zt_tgt
                    zt_tgt = x_tgt
                    vt_src = 0
                #target
                if context_options is not None:
                    counter = torch.zeros_like(zt_tgt, device=intermediate_device)
                    vt_tgt = torch.zeros_like(zt_tgt, device=intermediate_device)
                    context_queue = list(context(idx, steps, latent_video_length, context_frames, context_stride, context_overlap))
                    for c in context_queue:
                        window_id = self.window_tracker.get_window_id(c)

                        if teacache_args is not None:
                            current_teacache = self.window_tracker.get_teacache(window_id, self.teacache_state)
                        else:
                            current_teacache = None

                        prompt_index = min(int(max(c) / section_size), num_prompts - 1)
                        if context_options["verbose"]:
                            log.info(f"Prompt index: {prompt_index}")
                     
                        positive = text_embeds["prompt_embeds"][prompt_index]
                        
                        partial_img_emb = None
                        if image_cond is not None:
                            partial_img_emb = image_cond[:, c, :, :]
                            partial_img_emb[:, 0, :, :] = image_cond[:, 0, :, :].to(intermediate_device)

                        partial_zt_tgt = zt_tgt[:, c, :, :]
                        vt_tgt_context, new_teacache = predict_with_cfg(
                            partial_zt_tgt, cfg[idx], 
                            positive, text_embeds["negative_prompt_embeds"],
                            timestep, idx, partial_img_emb, 
                            clip_fea, current_teacache)
                        
                        if teacache_args is not None:
                            self.window_tracker.teacache_states[window_id] = new_teacache
                        
                        window_mask = create_window_mask(vt_tgt_context, c, latent_video_length, context_overlap)
                        vt_tgt[:, c, :, :] += vt_tgt_context * window_mask
                        counter[:, c, :, :] += window_mask
                    vt_tgt /= counter
                else:
                    vt_tgt, self.teacache_state = predict_with_cfg(
                        zt_tgt, cfg[idx], 
                        text_embeds["prompt_embeds"][0], 
                        text_embeds["negative_prompt_embeds"], 
                        timestep, idx, image_cond, clip_fea,
                        teacache_state=self.teacache_state)
                v_delta = vt_tgt - vt_src
                x_tgt = x_tgt.to(torch.float32)
                v_delta = v_delta.to(torch.float32)
                x_tgt = x_tgt + (sigma_prev - sigma) * v_delta
                x0 = x_tgt
            #context windowing
            elif context_options is not None:
                counter = torch.zeros_like(latent_model_input, device=intermediate_device)
                noise_pred = torch.zeros_like(latent_model_input, device=intermediate_device)
                context_queue = list(context(idx, steps, latent_video_length, context_frames, context_stride, context_overlap))
                
                for c in context_queue:
                    window_id = self.window_tracker.get_window_id(c)
                    
                    if teacache_args is not None:
                        current_teacache = self.window_tracker.get_teacache(window_id, self.teacache_state)
                    else:
                        current_teacache = None

                    prompt_index = min(int(max(c) / section_size), num_prompts - 1)
                    if context_options["verbose"]:
                        log.info(f"Prompt index: {prompt_index}")
                    
                    # Use the appropriate prompt for this section
                    positive = text_embeds["prompt_embeds"][prompt_index]

                    partial_img_emb = None
                    if image_cond is not None:
                        log.info(f"Image cond shape: {image_cond.shape}")
                        num_windows= context_options["image_cond_window_count"]
                        section_size = latent_video_length / num_windows
                        image_index = min(int(max(c) / section_size), num_windows - 1)
                        partial_img_emb = image_cond[:, c, :, :]
                        partial_image_cond = image_cond[:, 0, :, :].to(intermediate_device)
                        log.info(f"image_index: {image_index}")
                        if hasattr(self, "previous_noise_pred_context") and image_index > 0: #wip
                            if idx >= context_options["image_cond_start_step"]:
                                #strength = 0.5
                                #partial_image_cond *= strength
                                mask = torch.ones(4, partial_img_emb.shape[2], partial_img_emb.shape[3], device=partial_img_emb.device, dtype=partial_img_emb.dtype) #torch.Size([20, 10, 104, 60])
                                if context_vae is not None:
                                    to_decode = self.previous_noise_pred_context[:,-1,:, :].unsqueeze(1).unsqueeze(0).to(context_vae.dtype)
                                    #to_decode = to_decode.permute(0, 1, 3, 2)
                                    print("to_decode.shape", to_decode.shape)
                                    if isinstance(context_vae, TAEHV):
                                        image = context_vae.decode_video(to_decode.permute(0, 2, 1, 3, 4), parallel=False)
                                        print("image.shape", image.shape)
                                        image = context_vae.encode_video(image.repeat(1, 5, 1, 1, 1), parallel=False).permute(0, 2, 1, 3, 4)
                                    else:
                                        image = context_vae.decode(to_decode, device=device, tiled=False)[0]
                                        image = context_vae.encode(image.unsqueeze(0).to(context_vae.dtype), device=device, tiled=False)
                                    #print("decoded image.shape", image.shape) #torch.Size([3, 37, 832, 480])
                                    #print("encoded image.shape", image.shape)
                                    #partial_img_emb[:, 0, :, :] = image[0][:,0,:,:]                                        
                                    #print("partial_img_emb.shape", partial_img_emb.shape)
                                    #print("mask.shape", mask.shape)
                                    #print("self.previous_noise_pred_context.shape", self.previous_noise_pred_context.shape) #torch.Size([16, 10, 104, 60])
                                    partial_img_emb[:, 0, :, :] =  torch.cat([image[0][:,0,:,:], mask], dim=0)
                            else:
                                partial_img_emb[:, 0, :, :] =  partial_image_cond
                      
                    partial_latent_model_input = latent_model_input[:, c, :, :]

                    noise_pred_context, new_teacache = predict_with_cfg(
                        partial_latent_model_input, 
                        cfg[idx], positive, 
                        text_embeds["negative_prompt_embeds"], 
                        timestep, idx, partial_img_emb, clip_fea,
                        current_teacache)

                    # if callback is not None:
                    #     callback_latent = (noise_pred.to(t.device) * t / 1000).detach().permute(1,0,2,3)
                    #     callback(idx, callback_latent, None, steps)

                    if teacache_args is not None:
                        self.window_tracker.teacache_states[window_id] = new_teacache
                    if image_cond is not None and image_index > 0:
                        self.previous_noise_pred_context = noise_pred_context

                    window_mask = create_window_mask(noise_pred_context, c, latent_video_length, context_overlap, looped=is_looped)                    
                    noise_pred[:, c, :, :] += noise_pred_context * window_mask
                    counter[:, c, :, :] += window_mask
                noise_pred /= counter
            #normal inference
            else:
                noise_pred, self.teacache_state = predict_with_cfg(
                    latent_model_input, 
                    cfg[idx], 
                    text_embeds["prompt_embeds"][0], 
                    text_embeds["negative_prompt_embeds"], 
                    timestep, idx, image_cond, clip_fea,
                    teacache_state=self.teacache_state)

            if latent_shift_loop:
                #reverse latent shift
                if latent_shift_start_percent <= current_step_percentage <= latent_shift_end_percent:
                    noise_pred = torch.cat([noise_pred[:, latent_video_length - shift_idx:]] + [noise_pred[:, :latent_video_length - shift_idx]], dim=1)
                    shift_idx = (shift_idx + latent_skip) % latent_video_length
                
            
            if flowedit_args is None:
                latent = latent.to(intermediate_device)
                
                temp_x0 = sample_scheduler.step(
                    noise_pred.unsqueeze(0),
                    t,
                    latent.unsqueeze(0),
                    return_dict=False,
                    generator=seed_g)[0]
                latent = temp_x0.squeeze(0)

                x0 = latent.to(device)
                if callback is not None:
                    callback_latent = (latent_model_input - noise_pred.to(t.device) * t / 1000).detach().permute(1,0,2,3)
                    callback(idx, callback_latent, None, steps)
                else:
                    pbar.update(1)
                del latent_model_input, timestep
            else:
                if callback is not None:
                    callback_latent = (zt_tgt - vt_tgt.to(t.device) * t / 1000).detach().permute(1,0,2,3)
                    callback(idx, callback_latent, None, steps)
                else:
                    pbar.update(1)
                
        if teacache_args is not None:
            states = transformer.teacache_state.states
            state_names = {
                0: "conditional",
                1: "unconditional"
            }
            for pred_id, state in states.items():
                name = state_names.get(pred_id, f"prediction_{pred_id}")
                if 'skipped_steps' in state:
                    log.info(f"TeaCache skipped: {state['skipped_steps']} {name} steps")
            transformer.teacache_state.clear_all()

        if transformer.attention_mode == "spargeattn_tune":
            saved_state_dict = extract_sparse_attention_state_dict(transformer)
            torch.save(saved_state_dict, "sparge_wan.pt")
            save_torch_file(saved_state_dict, "sparge_wan.safetensors")

        if force_offload:
            if model["manual_offloading"]:
                transformer.to(offload_device)
                mm.soft_empty_cache()
                gc.collect()

        try:
            print_memory(device)
            torch.cuda.reset_peak_memory_stats(device)
        except:
            pass

        return ({
            "samples": x0.unsqueeze(0).cpu(), "looped": is_looped
            }, )
    
class WindowTracker:
    def __init__(self, verbose=False):
        self.window_map = {}  # Maps frame sequence to persistent ID
        self.next_id = 0
        self.teacache_states = {}  # Maps persistent ID to teacache state
        self.verbose = verbose
    
    def get_window_id(self, frames):
        key = tuple(sorted(frames))  # Order-independent frame sequence
        if key not in self.window_map:
            self.window_map[key] = self.next_id
            if self.verbose:
                log.info(f"New window pattern {key} -> ID {self.next_id}")
            self.next_id += 1
        return self.window_map[key]
    
    def get_teacache(self, window_id, base_state):
        if window_id not in self.teacache_states:
            if self.verbose:
                log.info(f"Initializing persistent teacache for window {window_id}")
            self.teacache_states[window_id] = base_state.copy()
        return self.teacache_states[window_id]

#region VideoDecode
class WanVideoDecode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                    "vae": ("WANVAE",),
                    "samples": ("LATENT",),
                    "enable_vae_tiling": ("BOOLEAN", {"default": False, "tooltip": "Drastically reduces memory use but may introduce seams"}),
                    "tile_x": ("INT", {"default": 272, "min": 64, "max": 2048, "step": 1, "tooltip": "Tile size in pixels, smaller values use less VRAM, may introduce more seams"}),
                    "tile_y": ("INT", {"default": 272, "min": 64, "max": 2048, "step": 1, "tooltip": "Tile size in pixels, smaller values use less VRAM, may introduce more seams"}),
                    "tile_stride_x": ("INT", {"default": 144, "min": 32, "max": 2048, "step": 32, "tooltip": "Tile stride in pixels, smaller values use less VRAM, may introduce more seams"}),
                    "tile_stride_y": ("INT", {"default": 128, "min": 32, "max": 2048, "step": 32, "tooltip": "Tile stride in pixels, smaller values use less VRAM, may introduce more seams"}),
                    },
                }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "decode"
    CATEGORY = "WanVideoWrapper"

    def decode(self, vae, samples, enable_vae_tiling, tile_x, tile_y, tile_stride_x, tile_stride_y):
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()
        mm.soft_empty_cache()
        latents = samples["samples"]
        vae.to(device)

        latents = latents.to(device = device, dtype = vae.dtype)

        mm.soft_empty_cache()

        is_looped = samples.get("looped", False)
        warmup_latent_count = 3

        if is_looped:
            latents = torch.cat([latents, latents[:, :, :warmup_latent_count]], dim=2)

        if isinstance(vae, TAEHV):            
            image = vae.decode_video(latents.permute(0, 2, 1, 3, 4))[0].permute(1, 0, 2, 3)
        else:
            image = vae.decode(latents, device=device, tiled=enable_vae_tiling, tile_size=(tile_x, tile_y), tile_stride=(tile_stride_x, tile_stride_y))[0]
            vae.model.clear_cache()
            image = (image - image.min()) / (image.max() - image.min())
        vae.to(offload_device)

        if is_looped:
            image = image[:, warmup_latent_count * 4:]
        
        mm.soft_empty_cache()

        
        image = torch.clamp(image, 0.0, 1.0)
        image = image.permute(1, 2, 3, 0).cpu().float()

        return (image,)

#region VideoEncode
class WanVideoEncode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                    "vae": ("WANVAE",),
                    "image": ("IMAGE",),
                    "enable_vae_tiling": ("BOOLEAN", {"default": False, "tooltip": "Drastically reduces memory use but may introduce seams"}),
                    "tile_x": ("INT", {"default": 272, "min": 64, "max": 2048, "step": 1, "tooltip": "Tile size in pixels, smaller values use less VRAM, may introduce more seams"}),
                    "tile_y": ("INT", {"default": 272, "min": 64, "max": 2048, "step": 1, "tooltip": "Tile size in pixels, smaller values use less VRAM, may introduce more seams"}),
                    "tile_stride_x": ("INT", {"default": 144, "min": 32, "max": 2048, "step": 32, "tooltip": "Tile stride in pixels, smaller values use less VRAM, may introduce more seams"}),
                    "tile_stride_y": ("INT", {"default": 128, "min": 32, "max": 2048, "step": 32, "tooltip": "Tile stride in pixels, smaller values use less VRAM, may introduce more seams"}),
                    },
                    "optional": {
                        "noise_aug_strength": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 10.0, "step": 0.001, "tooltip": "Strength of noise augmentation, helpful for leapfusion I2V where some noise can add motion and give sharper results"}),
                        "latent_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.001, "tooltip": "Additional latent multiplier, helpful for leapfusion I2V where lower values allow for more motion"}),
                        "mask": ("MASK", ),
                    }
                }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("samples",)
    FUNCTION = "encode"
    CATEGORY = "WanVideoWrapper"

    def encode(self, vae, image, enable_vae_tiling, tile_x, tile_y, tile_stride_x, tile_stride_y, noise_aug_strength=0.0, latent_strength=1.0, mask=None):
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        vae.to(device)

        image = (image.clone()).to(vae.dtype).to(device).unsqueeze(0).permute(0, 4, 1, 2, 3) # B, C, T, H, W
        if noise_aug_strength > 0.0:
            image = add_noise_to_reference_video(image, ratio=noise_aug_strength)

        if isinstance(vae, TAEHV):
            latents = vae.encode_video(image.permute(0, 2, 1, 3, 4), parallel=False)# B, T, C, H, W
            latents = latents.permute(0, 2, 1, 3, 4)
        else:
            latents = vae.encode(image * 2.0 - 1.0, device=device, tiled=enable_vae_tiling, tile_size=(tile_x, tile_y), tile_stride=(tile_stride_x, tile_stride_y))
            vae.model.clear_cache()
        if latent_strength != 1.0:
            latents *= latent_strength

        log.info(f"encoded latents shape {latents.shape}")

        if mask is None:
            vae.to(offload_device)
        else:
            #latent_mask = mask.clone().to(vae.dtype).to(device) * 2.0 - 1.0
            #latent_mask = latent_mask.unsqueeze(0).unsqueeze(0).repeat(1, 3, 1, 1, 1)
            #latent_mask = vae.encode(latent_mask, device=device, tiled=enable_vae_tiling, tile_size=(tile_x, tile_y), tile_stride=(tile_stride_x, tile_stride_y))
            target_h, target_w = latents.shape[3:]

            mask = torch.nn.functional.interpolate(
                mask.unsqueeze(0).unsqueeze(0),  # Add batch and channel dims [1,1,T,H,W]
                size=(latents.shape[2], target_h, target_w),
                mode='trilinear',
                align_corners=False
            ).squeeze(0)  # Remove batch dim, keep channel dim
            
            # Add batch & channel dims for final output
            latent_mask = mask.unsqueeze(0).repeat(1, latents.shape[1], 1, 1, 1)
            log.info(f"latent mask shape {latent_mask.shape}")
            vae.to(offload_device)
        mm.soft_empty_cache()
 
        return ({"samples": latents, "mask": latent_mask},)

class WanVideoLatentPreview:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "samples": ("LATENT",),
                 "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                 "min_val": ("FLOAT", {"default": -0.15, "min": -1.0, "max": 0.0, "step": 0.0001}),
                 "max_val": ("FLOAT", {"default": 0.15, "min": 0.0, "max": 1.0, "step": 0.0001}),
                 "r_bias": ("FLOAT", {"default": 0.0, "min": -1.0, "max": 1.0, "step": 0.0001}),
                 "g_bias": ("FLOAT", {"default": 0.0, "min": -1.0, "max": 1.0, "step": 0.0001}),
                 "b_bias": ("FLOAT", {"default": 0.0, "min": -1.0, "max": 1.0, "step": 0.0001}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING", )
    RETURN_NAMES = ("images", "latent_rgb_factors",)
    FUNCTION = "sample"
    CATEGORY = "WanVideoWrapper"

    def sample(self, samples, seed, min_val, max_val, r_bias, g_bias, b_bias):
        mm.soft_empty_cache()

        latents = samples["samples"].clone()
        print("in sample", latents.shape)
        #latent_rgb_factors =[[-0.02531045419704009, -0.00504800612542497, 0.13293717293982546], [-0.03421835830845858, 0.13996708548892614, -0.07081038680118075], [0.011091819063647063, -0.03372949685846012, -0.0698232210116172], [-0.06276524604742019, -0.09322986677909442, 0.01826383612148913], [0.021290659938126788, -0.07719530444034409, -0.08247812477766273], [0.04401102991215147, -0.0026401932105894754, -0.01410913586718443], [0.08979717602613707, 0.05361221258740831, 0.11501425309699129], [0.04695121980405198, -0.13053491609675175, 0.05025986885867986], [-0.09704684176098193, 0.03397687417738002, -0.1105886644677771], [0.14694697234804935, -0.12316902186157716, 0.04210404546699645], [0.14432470831243552, -0.002580008133591355, -0.08490676947390643], [0.051502750076553944, -0.10071695490292451, -0.01786223610178095], [-0.12503276881774464, 0.08877830923879379, 0.1076584501927316], [-0.020191205513213406, -0.1493425056303128, -0.14289740371758308], [-0.06470138952271293, -0.07410426095060325, 0.00980804676890873], [0.11747671720735695, 0.10916082743849789, -0.12235599365235904]]
        latent_rgb_factors = [
        [0.000159, -0.000223, 0.001299],
        [0.000566, 0.000786, 0.001948],
        [0.001531, -0.000337, 0.000863],
        [0.001887, 0.002190, 0.002117],
        [0.002032, 0.000782, -0.000512],
        [0.001634, 0.001260, 0.001685],
        [0.001360, -0.000292, 0.000189],
        [0.001410, 0.000769, 0.001935],
        [-0.000365, 0.000211, 0.000397],
        [-0.000091, 0.001333, 0.001812],
        [0.000201, 0.001866, 0.000546],
        [0.001889, 0.000544, -0.000237],
        [0.001779, 0.000022, 0.001764],
        [0.001456, 0.000431, 0.001574],
        [0.001791, 0.001738, -0.000121],
        [-0.000034, -0.000405, 0.000708]
    ]

        import random
        random.seed(seed)
        #latent_rgb_factors = [[random.uniform(min_val, max_val) for _ in range(3)] for _ in range(16)]
        #latent_rgb_factors = [[0.1 for _ in range(3)] for _ in range(16)]
        out_factors = latent_rgb_factors
        print(latent_rgb_factors)

        latent_rgb_factors_bias = [-0.0011, 0.0, -0.0002]
        #latent_rgb_factors_bias = [r_bias, g_bias, b_bias]

        latent_rgb_factors = torch.tensor(latent_rgb_factors, device=latents.device, dtype=latents.dtype).transpose(0, 1)
        latent_rgb_factors_bias = torch.tensor(latent_rgb_factors_bias, device=latents.device, dtype=latents.dtype)
        print(latent_rgb_factors)

        print("latent_rgb_factors", latent_rgb_factors.shape)

        latent_images = []
        for t in range(latents.shape[2]):
            latent = latents[:, :, t, :, :]
            latent = latent[0].permute(1, 2, 0)
            latent_image = torch.nn.functional.linear(
                latent,
                latent_rgb_factors,
                bias=latent_rgb_factors_bias
            )
            latent_images.append(latent_image)
        latent_images = torch.stack(latent_images, dim=0)
        print("latent_images", latent_images.shape)
        latent_images_min = latent_images.min()
        latent_images_max = latent_images.max()
        latent_images = (latent_images - latent_images_min) / (latent_images_max - latent_images_min)

        return (latent_images.float().cpu(), out_factors)

NODE_CLASS_MAPPINGS = {
    "WanVideoSampler": WanVideoSampler,
    "WanVideoDecode": WanVideoDecode,
    "WanVideoTextEncode": WanVideoTextEncode,
    "WanVideoModelLoader": WanVideoModelLoader,
    "WanVideoVAELoader": WanVideoVAELoader,
    "LoadWanVideoT5TextEncoder": LoadWanVideoT5TextEncoder,
    "WanVideoImageClipEncode": WanVideoImageClipEncode,
    "LoadWanVideoClipTextEncoder": LoadWanVideoClipTextEncoder,
    "WanVideoEncode": WanVideoEncode,
    "WanVideoBlockSwap": WanVideoBlockSwap,
    "WanVideoTorchCompileSettings": WanVideoTorchCompileSettings,
    "WanVideoLatentPreview": WanVideoLatentPreview,
    "WanVideoEmptyEmbeds": WanVideoEmptyEmbeds,
    "WanVideoLoraSelect": WanVideoLoraSelect,
    "WanVideoLoraBlockEdit": WanVideoLoraBlockEdit,
    "WanVideoEnhanceAVideo": WanVideoEnhanceAVideo,
    "WanVideoContextOptions": WanVideoContextOptions,
    "WanVideoTeaCache": WanVideoTeaCache,
    "WanVideoVRAMManagement": WanVideoVRAMManagement,
    "WanVideoTextEmbedBridge": WanVideoTextEmbedBridge,
    "WanVideoFlowEdit": WanVideoFlowEdit,
    "WanVideoControlEmbeds": WanVideoControlEmbeds,
    "WanVideoSLG": WanVideoSLG,
    "WanVideoTinyVAELoader": WanVideoTinyVAELoader,
    "WanVideoLoopArgs": WanVideoLoopArgs
    }
NODE_DISPLAY_NAME_MAPPINGS = {
    "WanVideoSampler": "WanVideo Sampler",
    "WanVideoDecode": "WanVideo Decode",
    "WanVideoTextEncode": "WanVideo TextEncode",
    "WanVideoTextImageEncode": "WanVideo TextImageEncode (IP2V)",
    "WanVideoModelLoader": "WanVideo Model Loader",
    "WanVideoVAELoader": "WanVideo VAE Loader",
    "LoadWanVideoT5TextEncoder": "Load WanVideo T5 TextEncoder",
    "WanVideoImageClipEncode": "WanVideo ImageClip Encode",
    "LoadWanVideoClipTextEncoder": "Load WanVideo Clip Encoder",
    "WanVideoEncode": "WanVideo Encode",
    "WanVideoBlockSwap": "WanVideo BlockSwap",
    "WanVideoTorchCompileSettings": "WanVideo Torch Compile Settings",
    "WanVideoLatentPreview": "WanVideo Latent Preview",
    "WanVideoEmptyEmbeds": "WanVideo Empty Embeds",
    "WanVideoLoraSelect": "WanVideo Lora Select",
    "WanVideoLoraBlockEdit": "WanVideo Lora Block Edit",
    "WanVideoEnhanceAVideo": "WanVideo Enhance-A-Video",
    "WanVideoContextOptions": "WanVideo Context Options",
    "WanVideoTeaCache": "WanVideo TeaCache",
    "WanVideoVRAMManagement": "WanVideo VRAM Management",
    "WanVideoTextEmbedBridge": "WanVideo TextEmbed Bridge",
    "WanVideoFlowEdit": "WanVideo FlowEdit",
    "WanVideoControlEmbeds": "WanVideo Control Embeds",
    "WanVideoSLG": "WanVideo SLG",
    "WanVideoTinyVAELoader": "WanVideo Tiny VAE Loader",
    "WanVideoLoopArgs": "WanVideo Loop Args"
    }
