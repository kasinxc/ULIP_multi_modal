#!/bin/bash

if [ -z "$1" ]; then
echo "Please provide a *.pt file as input"
exit 1
fi

model_file=$1
output_dir=./outputs/test_buildingnet_pointmlp_8kpts

CUDA_VISIBLE_DEVICES=0 python3 main.py --model ULIP_PN_MLP --npoints 8192 --output-dir $output_dir --evaluate_3d --pretrain_dataset_name buildingnet --validate_dataset_name buildingnet --test_ckpt_addr $model_file 2>&1 | tee $output_dir/log_buildingnet.txt
