#!/bin/bash
#SBATCH -J train_st3d_model               # Job name
#SBATCH -o train_st3d_model.%j            # Name of stdout output file (%j expands to jobId)
#SBATCH -p gpu-a100                        # Queue name
#SBATCH -N 1                               # Total number of nodes requested (128 cores/node)
#SBATCH -n 3                               # Total number of mpi tasks requested
#SBATCH -t 01:00:00                        # Run time (hh:mm:ss)
#SBATCH -A IRI23004                        # Allocation name

export SINGULARITYENV_CUDA_VISIBLE_DEVICES=0,1,2
module load cuda/11.3
module load tacc-apptainer
cd /work/09156/arthurz/research/ST3D/tools

export NUM_GPUS=3
export CUDA_VISIBLE_DEVICES=0,1,2

# singularity exec --nv st3d.sif bash scripts/dist_train.sh 3 --cfg_file cfgs/nuscenes_models/pvrcnn/pvrcnn_oracle.yaml --batch_size 3

#For nuscenes
export PORT1=29500
export CONFIG_FILE1=cfgs/nuscenes_models/pvrcnn/pvrcnn_oracle.yaml
export NUSCENES_EXTRA_TAG=pvrcnn_oracle


# Uncomment to launch nuscenes
module load launcher_gpu
export LAUNCHER_WORKDIR=/work/09156/arthurz/research/ST3D/tools
export LAUNCHER_JOB_FILE=scripts/launcher_train_nuscenes

${LAUNCHER_DIR}/paramrun