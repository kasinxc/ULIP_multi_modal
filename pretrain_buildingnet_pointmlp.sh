# CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 python3 -m torch.distributed.launch --nproc_per_node=8 
CUDA_VISIBLE_DEVICES=0 python3  main.py --model ULIP_PN_MLP --npoints 8192 --lr 1e-3 --output-dir ./outputs/reproduce_pointmlp_8kpts --pretrain_dataset_name buildingnet --validate_dataset_name buildingnet
