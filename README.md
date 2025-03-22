# :couple: Multi Process ComfyUI

- This project designed to run at some bare metal servers or workstations that have more than 2GPUs.
- This gonna run 2~8 ComfyUI instances that match your GPU count, that matches 1 instance by 1 GPU, so we can multi ComfyUI processing.
- Base image is from Pytorch Official Repository that CUDA already installed 

### ※ Prerequisites
#### docker, docker-compose, and git should be installed at your host and callable (each bin folders are added to OS path)



### 1. Clone repository
$ git clone https://github.com/bitflow-oss/multi-comfyui.git

``` text
- This directory will be container sharing host directory, so if you don't want this directory as base dir, move files to another directory.
- This directory will create 'custom_nodes', 'output', 'models', 'user' directory that each ComfyUI need to share.
- About binding container volume to host volume, refer 'docker-compose.yml' file here.
``` 

### 2. After cloning, change 2 lines at 'Dockerfile' describing torch and cuda version like below, if needed.

If torch 2.6 and CUDA 12.4 does not meet your needs, fix it to the version you required.

``` dockerfile
- FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-devel AS builder
- FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime
```

Find more image version from https://hub.docker.com/r/pytorch/pytorch/tags

### 3. Modify path & versions at the files below to meet your requirements
``` dockerfile
extra_model_paths.yaml
docker-compose-build.yml
docker-compose.yml
```

### 4. Build local docker image using Docker (docker-compose-build.xml)
``` shell
$ ./docker-build.sh
```

### 5. Run containers
``` shell
$ ./docker-run.sh
```

### 6. Check the shared directories between host and containser

UserName is USER, check the permission is set correctly Read/Write

<br/>

![Directory Example](https://imagedelivery.net/EAA0G_YMWOl4Q8zl4loPDA/2165e7a5-a03d-44b1-5563-64aad7ff1e00/none)

<br/>

### ps. Check and compare the requirements.txt file with the recent requirements.txt file at official ComfyUI github.
We've annotated Torch, Torch Vision, and Torch Audio because they are already installed in the official PyTorch image.
The requirements.txt file overrides the file at ComfyUI Github, 
so you'll need to check for and replace the latest requirements.txt file 
and then re-comment out torch, torch vision, torch audio.

<br/>

### If image is built wrong, remove image and re-build or change tag to another one (local use only)
