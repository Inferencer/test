import os
import torch
import yaml
import folder_paths
import comfy.model_management as mm
import comfy.utils
import numpy as np
import cv2
from tqdm import tqdm



script_directory = os.path.dirname(os.path.abspath(__file__))

from .liveportrait.live_portrait_pipeline import LivePortraitPipeline
from .liveportrait.utils.cropper import Cropper
from .liveportrait.modules.spade_generator import SPADEDecoder
from .liveportrait.modules.warping_network import WarpingNetwork
from .liveportrait.modules.motion_extractor import MotionExtractor
from .liveportrait.modules.appearance_feature_extractor import (
    AppearanceFeatureExtractor,
)
from .liveportrait.modules.stitching_retargeting_network import (
    StitchingRetargetingNetwork,
)

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

class InferenceConfig:
    def __init__(
        self,
        mask_crop=None,
        flag_use_half_precision=True,
        flag_lip_zero=True,
        lip_zero_threshold=0.03,
        flag_eye_retargeting=False,
        flag_lip_retargeting=False,
        flag_stitching=True,
        flag_relative=True,
        input_shape=(256, 256),
        flag_pasteback=True,
        device_id=0,
        flag_do_crop=True,
        flag_do_rot=True,
    ):
        self.flag_use_half_precision = flag_use_half_precision
        self.flag_lip_zero = flag_lip_zero
        self.lip_zero_threshold = lip_zero_threshold
        self.flag_eye_retargeting = flag_eye_retargeting
        self.flag_lip_retargeting = flag_lip_retargeting
        self.flag_stitching = flag_stitching
        self.flag_relative = flag_relative
        self.input_shape = input_shape
        self.flag_pasteback = flag_pasteback
        self.device_id = device_id
        self.flag_do_crop = flag_do_crop
        self.flag_do_rot = flag_do_rot
        self.mask_crop = mask_crop

class DownloadAndLoadLivePortraitModels:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {},
            "optional": {
                "precision": (
                    [
                        "fp16",
                        "fp32",
                        "auto",
                    ],
                    {"default": "auto"},
                ),
            },
        }

    RETURN_TYPES = ("LIVEPORTRAITPIPE",)
    RETURN_NAMES = ("live_portrait_pipe",)
    FUNCTION = "loadmodel"
    CATEGORY = "LivePortrait"

    def loadmodel(self, precision="fp16"):
        device = mm.get_torch_device()
        mm.soft_empty_cache()

        if precision == 'auto':
            try:
                if mm.is_device_mps(device):
                    print("LivePortrait using fp32 for MPS")
                    dtype = 'fp32'
                elif mm.should_use_fp16():
                    print("LivePortrait using fp16")
                    dtype = 'fp16'
                else:
                    print("LivePortrait using fp32")
                    dtype = 'fp32'
            except:
                raise AttributeError("ComfyUI version too old, can't autodetect properly. Set your dtypes manually.")
        else:
            dtype = precision
            print(f"LivePortrait using {dtype}")

        pbar = comfy.utils.ProgressBar(3)

        download_path = os.path.join(folder_paths.models_dir, "liveportrait")
        model_path = os.path.join(download_path)

        if not os.path.exists(model_path):
            log.info(f"Downloading model to: {model_path}")
            from huggingface_hub import snapshot_download

            snapshot_download(
                repo_id="Kijai/LivePortrait_safetensors",
                local_dir=download_path,
                local_dir_use_symlinks=False,
            )

        model_config_path = os.path.join(
            script_directory, "liveportrait", "config", "models.yaml"
        )
        with open(model_config_path, "r") as file:
            model_config = yaml.safe_load(file)

        feature_extractor_path = os.path.join(
            model_path, "appearance_feature_extractor.safetensors"
        )
        motion_extractor_path = os.path.join(model_path, "motion_extractor.safetensors")
        warping_module_path = os.path.join(model_path, "warping_module.safetensors")
        spade_generator_path = os.path.join(model_path, "spade_generator.safetensors")
        stitching_retargeting_path = os.path.join(
            model_path, "stitching_retargeting_module.safetensors"
        )

        # init F
        model_params = model_config["model_params"][
            "appearance_feature_extractor_params"
        ]
        self.appearance_feature_extractor = AppearanceFeatureExtractor(
            **model_params
        ).to(device)
        self.appearance_feature_extractor.load_state_dict(
            comfy.utils.load_torch_file(feature_extractor_path)
        )
        self.appearance_feature_extractor.eval()
        log.info("Load appearance_feature_extractor done.")
        pbar.update(1)
        # init M
        model_params = model_config["model_params"]["motion_extractor_params"]
        self.motion_extractor = MotionExtractor(**model_params).to(device)
        self.motion_extractor.load_state_dict(
            comfy.utils.load_torch_file(motion_extractor_path)
        )
        self.motion_extractor.eval()
        log.info("Load motion_extractor done.")
        pbar.update(1)
        # init W
        model_params = model_config["model_params"]["warping_module_params"]
        self.warping_module = WarpingNetwork(**model_params).to(device)
        self.warping_module.load_state_dict(
            comfy.utils.load_torch_file(warping_module_path)
        )
        self.warping_module.eval()
        log.info("Load warping_module done.")
        pbar.update(1)
        # init G
        model_params = model_config["model_params"]["spade_generator_params"]
        self.spade_generator = SPADEDecoder(**model_params).to(device)
        self.spade_generator.load_state_dict(
            comfy.utils.load_torch_file(spade_generator_path)
        )
        self.spade_generator.eval()
        log.info("Load spade_generator done.")
        pbar.update(1)

        def filter_checkpoint_for_model(checkpoint, prefix):
            """Filter and adjust the checkpoint dictionary for a specific model based on the prefix."""
            # Create a new dictionary where keys are adjusted by removing the prefix and the model name
            filtered_checkpoint = {
                key.replace(prefix + "_module.", ""): value
                for key, value in checkpoint.items()
                if key.startswith(prefix)
            }
            return filtered_checkpoint

        config = model_config["model_params"]["stitching_retargeting_module_params"]
        checkpoint = comfy.utils.load_torch_file(stitching_retargeting_path)

        stitcher_prefix = "retarget_shoulder"
        stitcher_checkpoint = filter_checkpoint_for_model(checkpoint, stitcher_prefix)
        stitcher = StitchingRetargetingNetwork(**config.get("stitching"))
        stitcher.load_state_dict(stitcher_checkpoint)
        stitcher = stitcher.to(device)
        stitcher.eval()

        lip_prefix = "retarget_mouth"
        lip_checkpoint = filter_checkpoint_for_model(checkpoint, lip_prefix)
        retargetor_lip = StitchingRetargetingNetwork(**config.get("lip"))
        retargetor_lip.load_state_dict(lip_checkpoint)
        retargetor_lip = retargetor_lip.to(device)
        retargetor_lip.eval()

        eye_prefix = "retarget_eye"
        eye_checkpoint = filter_checkpoint_for_model(checkpoint, eye_prefix)
        retargetor_eye = StitchingRetargetingNetwork(**config.get("eye"))
        retargetor_eye.load_state_dict(eye_checkpoint)
        retargetor_eye = retargetor_eye.to(device)
        retargetor_eye.eval()
        log.info("Load stitching_retargeting_module done.")

        self.stich_retargeting_module = {
            "stitching": stitcher,
            "lip": retargetor_lip,
            "eye": retargetor_eye,
        }

        pipeline = LivePortraitPipeline(
            self.appearance_feature_extractor,
            self.motion_extractor,
            self.warping_module,
            self.spade_generator,
            self.stich_retargeting_module,
            InferenceConfig(
                device_id=device,
                flag_use_half_precision=True if precision == "fp16" else False,
            ),
        )

        return (pipeline,)


class LivePortraitProcess:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {

            "pipeline": ("LIVEPORTRAITPIPE",),
            "crop_info": ("CROPINFO", {"default": []}),
            "source_image": ("IMAGE",),
            "driving_images": ("IMAGE",),
            "lip_zero": ("BOOLEAN", {"default": True}),
            "lip_zero_threshold": ("FLOAT", {"default": 0.03, "min": 0.001, "max": 4.0, "step": 0.001}),
            "eye_retargeting": ("BOOLEAN", {"default": False}),
            "eyes_retargeting_multiplier": ("FLOAT", {"default": 1.0, "min": 0.01, "max": 10.0, "step": 0.001}),
            "lip_retargeting": ("BOOLEAN", {"default": False}),
            "lip_retargeting_multiplier": ("FLOAT", {"default": 1.0, "min": 0.01, "max": 10.0, "step": 0.001}),
            "stitching": ("BOOLEAN", {"default": True}),
            "relative": ("BOOLEAN", {"default": True}),
            "mismatch_method": (
                    [
                        "constant",
                        "cycle",
                        "mirror",
                        "cut"
                    ],
                    {"default": "constant"},
                ),
            },
            "optional": {
                "mask": ("MASK", {"default": None}),
            }
        }

    RETURN_TYPES = (
        "IMAGE",
        "IMAGE",
        "MASK",
    )
    RETURN_NAMES = (
        "cropped_images",
        "full_images",
        "mask",
    )
    FUNCTION = "process"
    CATEGORY = "LivePortrait"

    def process(
        self,
        source_image: torch.Tensor,
        driving_images: torch.Tensor,
        crop_info: list,
        pipeline: LivePortraitPipeline,
        lip_zero: bool,
        lip_zero_threshold: float,
        eye_retargeting: bool,
        lip_retargeting: bool,
        stitching: bool,
        relative: bool,
        eyes_retargeting_multiplier: float,
        lip_retargeting_multiplier: float,
        mismatch_method: str = "constant",
        mask: torch.Tensor = None,
    ):
        if driving_images.shape[0] < source_image.shape[0]:
            raise ValueError("The number of driving images should be larger than the number of source images.")
        source_np = (source_image * 255).byte().numpy()
        
        pipeline.live_portrait_wrapper.cfg.flag_eye_retargeting = eye_retargeting
        pipeline.live_portrait_wrapper.cfg.eyes_retargeting_multiplier = (
            eyes_retargeting_multiplier
        )
        pipeline.live_portrait_wrapper.cfg.flag_lip_retargeting = lip_retargeting
        pipeline.live_portrait_wrapper.cfg.lip_retargeting_multiplier = (
            lip_retargeting_multiplier
        )
        pipeline.live_portrait_wrapper.cfg.flag_stitching = stitching
        pipeline.live_portrait_wrapper.cfg.flag_relative = relative
        pipeline.live_portrait_wrapper.cfg.flag_lip_zero = lip_zero
        pipeline.live_portrait_wrapper.cfg.lip_zero_threshold = lip_zero_threshold

        if lip_zero and (lip_retargeting or eye_retargeting):
            log.warning("Warning: lip_zero only has an effect with lip or eye retargeting")

        if mask is not None:
            crop_mask = mask[0].cpu().numpy()
            crop_mask = (crop_mask * 255).astype(np.uint8)
            crop_mask = np.repeat(np.atleast_3d(crop_mask), 3, axis=2)
            pipeline.live_portrait_wrapper.cfg.mask_crop = crop_mask
        else:
            log.info("Using default mask template")
            pipeline.live_portrait_wrapper.cfg.mask_crop = cv2.imread(os.path.join(script_directory, "liveportrait", "utils", "resources", "mask_template.png"), cv2.IMREAD_COLOR)

        driving_images_256 = comfy.utils.common_upscale(driving_images.permute(0, 3, 1, 2), 256, 256, "lanczos", "disabled")
        if pipeline.live_portrait_wrapper.cfg.flag_use_half_precision:
            driving_images_256 = driving_images_256.to(torch.float16)

        cropped_out_list = []
        full_out_list = []

        cropped_out_list, full_out_list, out_mask_list = pipeline.execute(
            source_np, 
            driving_images_256, 
            crop_info, 
            mismatch_method
        )
      
        cropped_out_tensors = torch.cat(cropped_out_list, dim=0)
        cropped_out_tensors = cropped_out_tensors
        
        full_tensors_out = (
            torch.stack([torch.from_numpy(np_array) for np_array in full_out_list])
            / 255
        )
        
        mask_tensors_out = (
            torch.stack([torch.from_numpy(np_array) for np_array in out_mask_list])
        )[:, :, :, 0]
        
        
        return (
            cropped_out_tensors.cpu().float(), 
            full_tensors_out.cpu().float(), 
            mask_tensors_out.cpu().float()
            )


class LivePortraitCropper:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {

            "source_image": ("IMAGE",),
            "dsize": ("INT", {"default": 512, "min": 64, "max": 2048}),
            "scale": ("FLOAT", {"default": 2.3, "min": 1.0, "max": 4.0, "step": 0.01}),
            "vx_ratio": ("FLOAT", {"default": 0.0, "min": -1.0, "max": 1.0, "step": 0.001}),
            "vy_ratio": ("FLOAT", {"default": -0.125, "min": -1.0, "max": 1.0, "step": 0.001}),
            "face_index": ("INT", {"default": 0, "min": 0, "max": 100}),
            "rotate": ("BOOLEAN", {"default": True}),
            "onnx_device": (
                    ['CPU', 'CUDA', 'ROCM'], {
                        "default": 'CPU'
                    }),
            "keep_model_loaded": ("BOOLEAN", {"default": True})
            },
            "optional": {
                "opt_driving_images": ("IMAGE",),
            }
           
        }

    RETURN_TYPES = ("IMAGE", "CROPINFO",)
    RETURN_NAMES = ("cropped_image", "crop_info",)
    FUNCTION = "process"
    CATEGORY = "LivePortrait"

    def process(self, source_image, dsize, scale, vx_ratio, vy_ratio, face_index, rotate, keep_model_loaded, onnx_device='CUDA', opt_driving_images=None):
        source_image_np = (source_image * 255).byte().numpy()

        cropper_init_config = {
            'keep_model_loaded': keep_model_loaded,
            'onnx_device': onnx_device
        }
        
        if not hasattr(self, 'cropper') or self.cropper is None or self.current_config != cropper_init_config:
            self.current_config = cropper_init_config
            self.cropper = Cropper(**cropper_init_config)

        crop_info_list = []
        cropped_images_list = []

        if opt_driving_images is not None:
            driving_images_np = (opt_driving_images * 255).byte().numpy()
            driving_landmark_list = []
       
        pbar = comfy.utils.ProgressBar(len(source_image_np))
        for i in tqdm(range(len(source_image_np)), desc='Detecting and cropping..', total=len(source_image_np)):
            crop_info = self.cropper.crop_single_image(source_image_np[i], dsize, scale, vy_ratio, vx_ratio, face_index, rotate)
            crop_info_list.append(crop_info)
            cropped_image = crop_info['img_crop_256x256']
            cropped_images_list.append(cropped_image)

            if opt_driving_images is not None:
                driving_crop_dict = self.cropper.crop_single_image(driving_images_np[i], dsize, scale, vy_ratio, vx_ratio, face_index, rotate)
                driving_landmark_list.append(driving_crop_dict['lmk_crop'])
              
            pbar.update(1)
        
        if not keep_model_loaded:
            self.cropper = None
            mm.soft_empty_cache()

        
        cropped_tensors_out = (
            torch.stack([torch.from_numpy(np_array) for np_array in cropped_images_list])
            / 255
        )

        crop_info_dict = {
            'crop_info_list': crop_info_list
        }

        if opt_driving_images is not None:
            crop_info_dict['driving_landmark_list'] = driving_landmark_list

        return (cropped_tensors_out, crop_info_dict)


class KeypointsToImage:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "crop_info": ("CROPINFO", {"default": []}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("keypoints_image",)
    FUNCTION = "drawkeypoints"
    CATEGORY = "LivePortrait"

    def drawkeypoints(self, crop_info):
        height, width = crop_info["crop_info_list"][0]['input_image_size']
        keypoints_img_list = []
        pbar = comfy.utils.ProgressBar(len(crop_info))
        for crop in crop_info["crop_info_list"]:
            keypoints = crop['lmk_crop'].copy()
            # Draw each landmark as a circle
            blank_image = np.zeros((height, width, 3), dtype=np.uint8) * 255
            for (x, y) in keypoints:
                # Ensure the coordinates are within the dimensions of the blank image
                if 0 <= x < width and 0 <= y < height:
                    cv2.circle(blank_image, (int(x), int(y)), radius=2, color=(0, 0, 255))

            keypoints_image = cv2.cvtColor(blank_image, cv2.COLOR_BGR2RGB)
            keypoints_img_list.append(keypoints_image)
            pbar.update(1)

        keypoints_img_tensor = (
            torch.stack([torch.from_numpy(np_array) for np_array in keypoints_img_list]) / 255).float()


        return (keypoints_img_tensor,)

class KeypointScaler:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                "crop_info": ("CROPINFO", {"default": {}}),
                "scale": ("FLOAT", {"default": 1.0, "min": 0.01, "max": 10.0, "step": 0.001}),
                "offset_x": ("INT", {"default": 0, "min": -1024, "max": 1024, "step": 1}),
                "offset_y": ("INT", {"default": 0, "min": -1024, "max": 1024, "step": 1}),

            }
        }

    RETURN_TYPES = ("CROPINFO", "IMAGE",)
    RETURN_NAMES = ("crop_info", "keypoints_image",)
    FUNCTION = "process"
    CATEGORY = "LivePortrait"

    def process(self, crop_info, offset_x, offset_y, scale):

        keypoints = crop_info['crop_info']['lmk_crop'].copy()

        # Create an offset array
        # Calculate the centroid of the keypoints
        centroid = keypoints.mean(axis=0)

        # Translate keypoints to origin by subtracting the centroid
        translated_keypoints = keypoints - centroid

        # Scale the translated keypoints
        scaled_keypoints = translated_keypoints * scale

        # Translate scaled keypoints back to original position and then apply the offset
        final_keypoints = scaled_keypoints + centroid + np.array([offset_x, offset_y])

        crop_info['crop_info']['lmk_crop'] = final_keypoints #fix this

        # Draw each landmark as a circle
        width, height = 512, 512
        blank_image = np.zeros((height, width, 3), dtype=np.uint8) * 255
        for (x, y) in final_keypoints:
            # Ensure the coordinates are within the dimensions of the blank image
            if 0 <= x < width and 0 <= y < height:
                cv2.circle(blank_image, (int(x), int(y)), radius=2, color=(0, 0, 255))

        keypoints_image = cv2.cvtColor(blank_image, cv2.COLOR_BGR2RGB)
        keypoints_image_tensor = torch.from_numpy(keypoints_image) / 255
        keypoints_image_tensor = keypoints_image_tensor.unsqueeze(0).cpu().float()
        
        return (crop_info, keypoints_image_tensor,)

NODE_CLASS_MAPPINGS = {
    "DownloadAndLoadLivePortraitModels": DownloadAndLoadLivePortraitModels,
    "LivePortraitProcess": LivePortraitProcess,
    "LivePortraitCropper": LivePortraitCropper,
    "KeypointScaler": KeypointScaler,
    "KeypointsToImage": KeypointsToImage
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "DownloadAndLoadLivePortraitModels": "(Down)Load LivePortraitModels",
    "LivePortraitProcess": "LivePortraitProcess",
    "LivePortraitCropper": "LivePortraitCropper",
    "KeypointScaler": "KeypointScaler",
    "KeypointsToImage": "LivePortrait KeypointsToImage"
    }