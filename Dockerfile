# ------------------- Stage 1: Build Stage ------------------------------
FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-devel AS builder

RUN mkdir models user

RUN apt-get update
RUN apt-get install -y git

WORKDIR /code

# Copy the current directory contents into the container at $HOME/app setting the owner to the user
RUN git clone https://github.com/comfyanonymous/ComfyUI

RUN cd /code/ComfyUI/custom_nodes && git clone https://github.com/ltdrdata/ComfyUI-Manager

RUN pip3 wheel --no-cache-dir --no-deps --wheel-dir /app/wheels -r /code/ComfyUI/custom_nodes/ComfyUI-Manager/requirements.txt

RUN cd /code/ComfyUI

# To ignore re-install pytorch that already in the container image, use local requirements.txt that commented out pytorch
COPY requirements.txt extra_model_paths.yaml /code/

# Set the working directory to /code
WORKDIR /code

# Install dependencies specified in requirements.txt
RUN pip3 wheel --no-cache-dir --no-deps --wheel-dir /app/wheels -r /code/ComfyUI/requirements.txt

# Install Checkpoints (selective)
RUN echo "Downloading checkpoints..."
#RUN wget -c https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors -P ./models/checkpoints/
# Install Custom nodes (selective)
RUN echo "Installing custom nodes..."
#RUN cd custom_nodes && git clone https://github.com/Fannovel16/comfy_controlnet_preprocessors && cd comfy_controlnet_preprocessors && python install.py --no_download_ckpts
RUN echo "Build Finished"

# ------------------- Stage 2: Final Stage ------------------------------

# Use a slim Python 3.11 image as the final base image
FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

# Set the working directory to /app
WORKDIR /app

# Copy the built dependencies from the builder stage
COPY --from=builder /app/wheels /wheels

RUN echo "Install Dependencies to Runtime container"
RUN pip3 install --no-cache /wheels/*

# Copy the application code from the builder stage
RUN echo "Copy source codes to Runtime container"
COPY --from=builder /code/ComfyUI /app

# Expose port 8188 for the ComfyUI application
EXPOSE 8188

RUN echo "Finished building ComfyUI container image"
