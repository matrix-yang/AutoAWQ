import os
import gc
import json
import torch
import logging
import functools
import torch.nn as nn
from tqdm import tqdm
from typing import List, Union
from collections import defaultdict
from safetensors.torch import save_file

from awq.modules.act import ScaledActivation
from huggingface_hub import snapshot_download
from awq.utils.utils import simple_dispatch_model
from awq.utils.calib_data import get_calib_dataset
from transformers.modeling_utils import shard_checkpoint
from awq.quantize.quantizer import pseudo_quantize_tensor
from awq.modules.linear import WQLinear_GEMM, WQLinear_GEMV
from awq.quantize.auto_clip import auto_clip_block, apply_clip
from awq.quantize.auto_scale import auto_scale_block, apply_scale
from transformers import AutoModelForCausalLM, AutoConfig, PreTrainedModel
from accelerate import init_empty_weights, load_checkpoint_in_model, infer_auto_device_map
from awq.utils.module import append_str_prefix, get_op_name, get_named_linears, set_op_by_name

class BaseAWQForCausalLM(nn.Module):
    def __init__(self, model, model_type, is_quantized, quant_config):
        super().__init__()
        self.model:PreTrainedModel = model
        self.model_type:str = model_type
        self.is_quantized:bool = is_quantized
        self.search_result = None
        self.quant_config:dict = quant_config
    
    def to(self, device: str):
        return self.model.to(device)
    
    def forward(self, *args, **kwargs):
        return self.model(*args, **kwargs)
    
    def generate(self, *args, **kwargs):
        with torch.inference_mode():
            return self.model.generate(*args, **kwargs)

    @torch.no_grad()
    def quantize(self, tokenizer=None, quant_config={}, n_samples=128, seqlen=512,
                       auto_scale=True, mse_range=True, run_search=True, run_quant=True,
                       calib_data: Union[str, List[str]]="pileval", split="train",
                       text_column="content"):
        self.quant_config = quant_config
        quant_config["version"] = "GEMM" if 'version' not in quant_config.keys() else quant_config["version"]

        if run_search:
            self.search_result = self._awq_search(
                tokenizer, quant_config, n_samples=n_samples, seqlen=seqlen,
                auto_scale=auto_scale, mse_range=mse_range, calib_data=calib_data,
                split=split, text_column=text_column
            )
        
        if run_quant:
            self._awq_quant()
            self.is_quantized = True
    
    @staticmethod
    def fuse_layers(model, quant_config):
        pass
        
    def _awq_quant(self):
        assert self.quant_config["zero_point"], "We only support zero_point quantization now."
        layers = self.get_model_layers(self.model)

        # Run AWQ quantization
        for i in tqdm(range(len(layers)), desc="AWQ Quantization"):
            layer = layers[i]
            named_linears = get_named_linears(layer)
            self._scale_activations(self, layer)

            for name, module in named_linears.items():
                module.cuda()

                module.weight.data, scales, zeros = pseudo_quantize_tensor(
                    module.weight.data, 
                    get_scale_zp=True, 
                    w_bit=self.quant_config["w_bit"], 
                    q_group_size=self.quant_config["q_group_size"]
                )

                if self.quant_config["version"] == 'GEMM':
                    scales = scales.t().contiguous()
                    zeros = zeros.t().contiguous()
                    q_linear_module = WQLinear_GEMM
                elif self.quant_config["version"] == 'GEMV':
                    q_linear_module = WQLinear_GEMV
                
                q_linear = q_linear_module.from_linear(
                    module,
                    self.quant_config['w_bit'],
                    self.quant_config['q_group_size'],
                    False,
                    scales,
                    zeros
                )

                module.cpu()
                q_linear.to(next(layer.parameters()).device)
                set_op_by_name(layer, name, q_linear)
                torch.cuda.empty_cache()
                gc.collect()
            
            torch.cuda.empty_cache()
            gc.collect()
    
    def _awq_search(self, tokenizer, quant_config, n_samples=128, seqlen=512,
                       auto_scale=True, mse_range=True, calib_data:Union[str, List[str]]="pileval",
                       split="train", text_column="content"):
        layers = self.get_model_layers(self.model)

        samples = get_calib_dataset(
            data=calib_data, tokenizer=tokenizer, n_samples=n_samples, block_size=seqlen,
            split=split, text_column=text_column
        )
        samples = torch.cat(samples, dim=0)

        inps = []
        layer_kwargs = {}

        layers[0] = layers[0].cuda()
        self.move_embed(self.model, "cuda")
        
        # get input and kwargs to layer 0
        # with_kwargs is only supported in PyTorch 2.0
        # use this Catcher hack for now
        class Catcher(nn.Module):
            def __init__(self, module):
                super().__init__()
                self.module = module

            def forward(self, hijacked_inputs, **kwargs):
                inps.append(hijacked_inputs)
                layer_kwargs.update(kwargs)
                raise ValueError  # early exit to break later inference

        # patch layer 0 to catch input and kwargs
        layers[0] = Catcher(layers[0])
        try:
            self.model(samples.to(next(self.model.parameters()).device))
        except ValueError:  # work with early exit
            pass
        del samples
        layers[0] = layers[0].module  # restore
        inps = inps[0]

        layers[0] = layers[0].cpu()
        self.move_embed(self.model, "cpu")
        
        gc.collect()
        torch.cuda.empty_cache()
        awq_results = {
            "scale": [],
            "clip": [],
        }

        def clear_other_layer(layers,i):
            #mins=max(0,i-10)
            #maxs=min(79,i+10)
            for idx in range(10):
                layer = layers[idx]
                layers[idx]=layer.to('cpu')

            ten=int(i/10)*10

            #print(f"idx is {i} clear layer 0-10 {ten}-{ten+10}")
            for i in range(ten,ten+10):
                layer = layers[i]
                layers[i]=layer.to('cpu')
            torch.cuda.empty_cache()

        # Run AWQ search layer by layer
        for i in tqdm(range(len(layers)), desc="AWQ Search"):

            clear_other_layer(layers, i)
            dev = f'cuda:{int(i / 10)}'
            layer = layers[i]
            layer = layer.to(dev)
            named_linears = get_named_linears(layer)

            # firstly, get input features of all linear layers
            def cache_input_hook(m, x, y, name, feat_dict):
                x = x[0]
                x = x.detach().cpu()
                feat_dict[name].append(x)

            input_feat = defaultdict(list)
            handles = []
            for name in named_linears:
                handles.append(named_linears[name].register_forward_hook(
                    functools.partial(cache_input_hook, name=name,
                                    feat_dict=input_feat)))
            inps = inps.to(dev)  # in case multi-gpu
            # get output as next layer's input
            inps = layer(inps, **layer_kwargs)[0]
            for h in handles:
                h.remove()
            # now solve for scaling and clipping
            input_feat = {k: torch.cat(v, dim=0) for k, v in input_feat.items()}

            # Clear GPU memory
            torch.cuda.empty_cache()

            if auto_scale:  # if it applies, we should also modify the input_feat with scales
                scales_list = auto_scale_block(
                    self,
                    layer,
                    layer_kwargs,
                    quant_config=quant_config,
                    input_feat=input_feat,
                )

                apply_scale(layers[i], scales_list, input_feat_dict=input_feat)

                # append prefix to make names global
                awq_results["scale"] += append_str_prefix(scales_list, get_op_name(self.model, layer) + ".")

            # Clear GPU memory
            torch.cuda.empty_cache()
            
            if mse_range:
                clip_list = auto_clip_block(
                    layer,
                    quant_config=quant_config,
                    input_feat=input_feat
                )

                apply_clip(layer, clip_list)
                # append prefix to make names global
                awq_results["clip"] += append_str_prefix(clip_list, get_op_name(self.model, layer) + ".")

            layer = layer.cpu()
            # Haotian: check activation replacement
            del input_feat
            gc.collect()
            torch.cuda.empty_cache()
        
        return awq_results

    def save_quantized(self, save_dir, safetensors=False, shard_size="10GB"):
        def _save_files(save_dir, model_name='', search_result=None):
            class EmptyModule(nn.Module):
                def __init__(self): super(EmptyModule, self).__init__()
                def forward(self, x): return x

            # Save model files with empty state dict
            self.model.save_pretrained(save_dir, state_dict=EmptyModule().state_dict())

            # Remove empty state dict
            os.remove(f'{save_dir}/pytorch_model.bin')

            if search_result is not None:
                torch.save(search_result, f'{save_dir}/{model_name}')
            else:
                # model_name has no extension, add it when saving state_dict
                model_name = 'model.safetensors' if safetensors else 'pytorch_model.bin'

                # shard checkpoint into chunks (10GB default)
                shards, index = shard_checkpoint(
                    self.model.state_dict(), 
                    max_shard_size=shard_size, 
                    weights_name=model_name
                )

                for shard_file, shard in shards.items():
                    if safetensors:
                        # safetensors must be in the same memory, so we duplicate and use contiguous memory
                        shard = {k: v.clone().contiguous() for k, v in shard.items()}
                        save_file(shard, os.path.join(save_dir, shard_file), metadata={"format": "pt"})
                    else:
                        torch.save(shard, os.path.join(save_dir, shard_file))

                # save shard index
                if index is not None:
                    with open(f'{save_dir}/{model_name}.index.json', 'w+') as file:
                        file.write(json.dumps(index, indent=4))

            # Save config
            with open(f'{save_dir}/quant_config.json', 'w+') as file:
                file.write(json.dumps(self.quant_config, indent=4))

        save_dir = save_dir[:-1] if save_dir[-1] == '/' else save_dir

        # Save model
        if self.search_result is None or self.is_quantized:
            _save_files(save_dir, '', search_result=None)
        else:
            model_name = 'awq_model_search_result.pt'
            _save_files(save_dir, model_name, self.search_result)
        
    @classmethod
    def from_pretrained(self, model_path, model_type, torch_dtype: torch.dtype = torch.float16, 
                        trust_remote_code=True, safetensors=False):
        return self.from_quantized(
            model_path, 
            model_type, 
            model_filename='', 
            max_new_tokens=None,
            device='balanced', 
            torch_dtype=torch_dtype, 
            trust_remote_code=trust_remote_code, 
            safetensors=safetensors,
            is_quantized=False
        )

    @classmethod
    def from_quantized(self, model_path, model_type, model_filename='', 
                             max_new_tokens=None, device='balanced', torch_dtype=torch.float16, 
                             trust_remote_code=True, safetensors=False, is_quantized=True, 
                             fuse_layers=False, version='GEMM'):
        # [STEP 1] Download model if path is not a directory
        if not os.path.isdir(model_path):
            ignore_patterns = ["*msgpack*", "*h5*"]
            if safetensors:
                ignore_patterns.extend(["*.pt*", "*.bin*"])
            else:
                ignore_patterns.append("*.safetensors*")
            
            model_path = snapshot_download(model_path, ignore_patterns=ignore_patterns)
        
        if model_filename != '':
            model_weights_path = model_path + f'/{model_filename}'
        else:
            model_weights_path = model_path

        # [STEP 2] Load config and set sequence length
        # TODO: Create BaseAWQConfig class
        quant_config_path = f'{model_path}/quant_config.json'
        if os.path.exists(quant_config_path):
            with open(quant_config_path, 'r') as file:
                quant_config = json.loads(file.read())
            
            if "version" not in quant_config.keys():
                quant_config["version"] = version
        else:
            # Default config that works for most models
            quant_config = {"zero_point": True, "q_group_size": 128, "w_bit": 4, "version": version}
        
        # Load model config and set max generation length
        if max_new_tokens is None and hasattr(self, 'max_new_tokens_key'):
            config = AutoConfig.from_pretrained(model_path, trust_remote_code=trust_remote_code)
            config.max_new_tokens = getattr(config, self.max_new_tokens_key)
        else:
            max_new_tokens = 2048 if max_new_tokens is None else max_new_tokens
            config = AutoConfig.from_pretrained(model_path, trust_remote_code=trust_remote_code)
            config.max_new_tokens = max_new_tokens
        
        # [STEP 3] Load model
        with init_empty_weights():
            model = AutoModelForCausalLM.from_config(config=config, torch_dtype=torch_dtype, trust_remote_code=trust_remote_code)

        # Only need to replace layers if a model is AWQ quantized
        if is_quantized:
            # Prepare WQLinear layers, replace nn.Linear
            self._load_quantized_modules(self, model, quant_config, quant_config["version"])
        
        model.tie_weights()

        device_map = infer_auto_device_map(
            model,
            no_split_module_classes=[self.layer_type],
            dtype=torch_dtype
        )



        # Load model weights
        if is_quantized:
            device_map={'model.embed_tokens': 0, 'model.layers.0': 0, 'model.layers.1': 0, 'model.layers.2': 0,
             'model.layers.3': 0, 'model.layers.4': 0, 'model.layers.5': 0, 'model.layers.6': 0, 'model.layers.7': 0,
             'model.layers.8': 0, 'model.layers.9': 0, 'model.layers.10': 0, 'model.layers.11': 0, 'model.layers.12': 0,
             'model.layers.13': 0, 'model.layers.14': 0, 'model.layers.15': 0, 'model.layers.16': 0,
             'model.layers.17': 0, 'model.layers.18': 0, 'model.layers.19': 0, 'model.layers.20': 1,
             'model.layers.21': 1, 'model.layers.22': 1, 'model.layers.23': 1, 'model.layers.24': 1,
             'model.layers.25': 1, 'model.layers.26': 1, 'model.layers.27': 1, 'model.layers.28': 1,
             'model.layers.29': 1, 'model.layers.30': 1, 'model.layers.31': 1, 'model.layers.32': 1,
             'model.layers.33': 1, 'model.layers.34': 1, 'model.layers.35': 1, 'model.layers.36': 1,
             'model.layers.37': 1, 'model.layers.38': 1, 'model.layers.39': 1, 'model.layers.40': 2,
             'model.layers.41': 2, 'model.layers.42': 2, 'model.layers.43': 2, 'model.layers.44': 2,
             'model.layers.45': 2, 'model.layers.46': 2, 'model.layers.47': 2, 'model.layers.48': 2,
             'model.layers.49': 2, 'model.layers.50': 2, 'model.layers.51': 2, 'model.layers.52': 2,
             'model.layers.53': 2, 'model.layers.54': 2, 'model.layers.55': 2, 'model.layers.56': 2,
             'model.layers.57': 2, 'model.layers.58': 2, 'model.layers.59': 2, 'model.layers.60': 3,
             'model.layers.61': 3, 'model.layers.62': 3, 'model.layers.63': 3, 'model.layers.64': 3,
             'model.layers.65': 3, 'model.layers.66': 3, 'model.layers.67': 3, 'model.layers.68': 3,
             'model.layers.69': 3, 'model.layers.70': 3, 'model.layers.71': 3, 'model.layers.72': 3,
             'model.layers.73': 3, 'model.layers.74': 3, 'model.layers.75': 3, 'model.layers.76': 3,
             'model.layers.77': 3, 'model.layers.78': 3, 'model.layers.79': 3, 'model.norm': 3, 'lm_head': 3}
            load_checkpoint_in_model(
                model,
                checkpoint=model_weights_path,
                device_map=device_map
            )
            
            model = simple_dispatch_model(model, device_map)
            
            if fuse_layers:
                self.fuse_layers(model, quant_config)

        else:
            # If not quantized, must load with AutoModelForCausalLM
            del model
            device_map={'model.embed_tokens': 0, 'model.layers.0': 0, 'model.layers.1': 0, 'model.layers.2': 0, 'model.layers.3': 0, 'model.layers.4': 0, 'model.layers.5': 0, 'model.layers.6': 0, 'model.layers.7': 0, 'model.layers.8': 0, 'model.layers.9': 0, 'model.layers.10': 1, 'model.layers.11': 1, 'model.layers.12': 1, 'model.layers.13': 1, 'model.layers.14': 1, 'model.layers.15': 1, 'model.layers.16': 1, 'model.layers.17': 1, 'model.layers.18': 1, 'model.layers.19': 1, 'model.layers.20': 2, 'model.layers.21': 2, 'model.layers.22': 2, 'model.layers.23': 2, 'model.layers.24': 2, 'model.layers.25': 2, 'model.layers.26': 2, 'model.layers.27': 2, 'model.layers.28': 2, 'model.layers.29': 2, 'model.layers.30': 3, 'model.layers.31': 3, 'model.layers.32': 3, 'model.layers.33': 3, 'model.layers.34': 3, 'model.layers.35': 3, 'model.layers.36': 3, 'model.layers.37': 3, 'model.layers.38': 3, 'model.layers.39': 3, 'model.layers.40': 4, 'model.layers.41': 4, 'model.layers.42': 4, 'model.layers.43': 4, 'model.layers.44': 4, 'model.layers.45': 4, 'model.layers.46': 4, 'model.layers.47': 4, 'model.layers.48': 4, 'model.layers.49': 4, 'model.layers.50': 5, 'model.layers.51': 5, 'model.layers.52': 5, 'model.layers.53': 5, 'model.layers.54': 5, 'model.layers.55': 5, 'model.layers.56': 5, 'model.layers.57': 5, 'model.layers.58': 5, 'model.layers.59': 5, 'model.layers.60': 6, 'model.layers.61': 6, 'model.layers.62': 6, 'model.layers.63': 6, 'model.layers.64': 6, 'model.layers.65': 6, 'model.layers.66': 6, 'model.layers.67': 6, 'model.layers.68': 6, 'model.layers.69': 6, 'model.layers.70': 7, 'model.layers.71': 7, 'model.layers.72': 7, 'model.layers.73': 7, 'model.layers.74': 7, 'model.layers.75': 7, 'model.layers.76': 7, 'model.layers.77': 7, 'model.layers.78': 7, 'model.layers.79': 7, 'lm_head': 7, 'model.norm': 7}
            # Load model weights
            model = AutoModelForCausalLM.from_pretrained(
                model_weights_path, 
                device_map=device_map, 
                trust_remote_code=trust_remote_code, 
                offload_folder="offload", 
                offload_state_dict=True, 
                torch_dtype=torch_dtype, 
                use_safetensors=safetensors
            )
            model.eval()

        return self(model, model_type, is_quantized=is_quantized, quant_config=quant_config)

    def _load_quantized_modules(self, model, quant_config, version):
        # Real quantization of weights
        assert quant_config["zero_point"], "We only support zero_point quantization now."
        
        # Get blocks of model
        layers = self.get_model_layers(model)

        for i in tqdm(range(len(layers)), desc="Replacing layers..."):
            layer = layers[i]

            # Get every linear layer in a block
            named_linears = get_named_linears(layer)

            # Replace activation functions
            self._scale_activations(self, layer)

            # Replace nn.Linear with WQLinear
            for name, module in named_linears.items():
                if version == 'GEMM':
                    q_linear_module = WQLinear_GEMM
                elif version == 'GEMV':
                    q_linear_module = WQLinear_GEMV
                
                q_linear = q_linear_module.from_linear(
                    module,
                    quant_config['w_bit'],
                    quant_config['q_group_size'],
                    True
                )
                q_linear.to(next(layer.parameters()).device)
                set_op_by_name(layer, name, q_linear)
            
            torch.cuda.empty_cache()
            gc.collect()
    
    @staticmethod
    def _scale_activations(self, layer):
        scale_dict = self.get_act_for_scaling(layer)

        if scale_dict['is_scalable']:
            if not isinstance(scale_dict['scale_layer'], ScaledActivation):
                param = next(layer.parameters())

                # get activation scale
                scale_like = torch.ones(scale_dict['scale_shape'], dtype=param.dtype, device=param.device)

                # scale activation
                scaled_act = ScaledActivation(scale_dict['scale_layer'], scale_like)
                set_op_by_name(layer, scale_dict['scale_name'], scaled_act)
