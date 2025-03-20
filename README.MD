# Multi Process ComfyUI

- This project started to run multi ComfyUI to match each GPU count.
- Using docker, runs 2~8 ComfyUI images.
- Base image is Pytorch official image that CUDA already installed 

### 1. Clone this repository
$ https://github.com/bitflow-oss/multi-comfyui.git

### 2. After clone this, need to change 2 line at 'Dockerfile' describing torch and cuda version like below.

- FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-devel AS builder
- FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

Fix it to the version you required. 
Find more image version from https://hub.docker.com/r/pytorch/pytorch/tags

### 3. Modify path & versions at the files below to meet your requirements
- extra_model_paths.yaml , docker-compose-build.yml , docker-compose.yml

### 4. Build local docker image using Dockerfile (docker-compose-build.xml)
$ ./docker-build.sh

### 5. Run docker-compose
$ ./docker-run.sh

### ps. requirements.txt file is the recent requirements.txt from ComfyUI github.
We've annotated Torch, Torch Vision, and Torch Audio because they are already installed in the official PyTorch image.
The requirements.txt file overrides the file at ComfyUI Github, 
so you'll need to check for and replace the latest requirements.txt file 
and then re-comment out torch, torch vision, torch audio.

# If image is built wrong, remove image and re-build or change tag to another one (local use only)