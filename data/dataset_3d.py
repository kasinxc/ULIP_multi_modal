'''
 * Copyright (c) 2023, salesforce.com, inc.
 * All rights reserved.
 * SPDX-License-Identifier: BSD-3-Clause
 * For full license text, see the LICENSE file in the repo root or https://opensource.org/licenses/BSD-3-Clause
 * By Le Xue
'''

import random

import torch
import numpy as np
import torch.utils.data as data

import yaml
from easydict import EasyDict

from utils.io import IO
from utils.build import DATASETS
from utils.logger import *
from utils.build import build_dataset_from_cfg
import json
from tqdm import tqdm
import pickle
from PIL import Image
from plyfile import PlyData

def pil_loader(path):
    # open path as file to avoid ResourceWarning (https://github.com/python-pillow/Pillow/issues/835)
    # print_log(f'[image: - pil_loader]', logger='BuildingNet')
    with open(path, 'rb') as f:
    # with open(path, 'r') as f:
        # print_log(f'[image: - open success]', logger='BuildingNet')
        img = Image.open(f)
        # print_log(f'[image: - image open success]', logger='BuildingNet')
        return img.convert('RGB')

def pc_normalize(pc):
    centroid = np.mean(pc, axis=0)
    pc = pc - centroid
    m = np.max(np.sqrt(np.sum(pc**2, axis=1)))
    pc = pc / m
    return pc

def farthest_point_sample(point, npoint):
    """
    Input:
        xyz: pointcloud data, [N, D]
        npoint: number of samples
    Return:
        centroids: sampled pointcloud index, [npoint, D]
    """
    N, D = point.shape
    xyz = point[:,:3]
    centroids = np.zeros((npoint,))
    distance = np.ones((N,)) * 1e10
    farthest = np.random.randint(0, N)
    for i in range(npoint):
        centroids[i] = farthest
        centroid = xyz[farthest, :]
        dist = np.sum((xyz - centroid) ** 2, -1)
        mask = dist < distance
        distance[mask] = dist[mask]
        farthest = np.argmax(distance, -1)
    point = point[centroids.astype(np.int32)]
    return point

def rotate_point_cloud(batch_data):
    """ Randomly rotate the point clouds to augument the dataset
        rotation is per shape based along up direction
        Input:
          BxNx3 array, original batch of point clouds
        Return:
          BxNx3 array, rotated batch of point clouds
    """
    rotated_data = np.zeros(batch_data.shape, dtype=np.float32)
    for k in range(batch_data.shape[0]):
        rotation_angle = np.random.uniform() * 2 * np.pi
        cosval = np.cos(rotation_angle)
        sinval = np.sin(rotation_angle)
        rotation_matrix = np.array([[cosval, 0, sinval],
                                    [0, 1, 0],
                                    [-sinval, 0, cosval]])
        shape_pc = batch_data[k, ...]
        rotated_data[k, ...] = np.dot(shape_pc.reshape((-1, 3)), rotation_matrix)
    return rotated_data

def random_point_dropout(batch_pc, max_dropout_ratio=0.875):
    ''' batch_pc: BxNx3 '''
    for b in range(batch_pc.shape[0]):
        dropout_ratio =  np.random.random()*max_dropout_ratio # 0~0.875
        drop_idx = np.where(np.random.random((batch_pc.shape[1]))<=dropout_ratio)[0]
        if len(drop_idx)>0:
            batch_pc[b,drop_idx,:] = batch_pc[b,0,:] # set to the first point
    return batch_pc

def random_scale_point_cloud(batch_data, scale_low=0.8, scale_high=1.25):
    """ Randomly scale the point cloud. Scale is per point cloud.
        Input:
            BxNx3 array, original batch of point clouds
        Return:
            BxNx3 array, scaled batch of point clouds
    """
    B, N, C = batch_data.shape
    scales = np.random.uniform(scale_low, scale_high, B)
    for batch_index in range(B):
        batch_data[batch_index,:,:] *= scales[batch_index]
    return batch_data

def shift_point_cloud(batch_data, shift_range=0.1):
    """ Randomly shift point cloud. Shift is per point cloud.
        Input:
          BxNx3 array, original batch of point clouds
        Return:
          BxNx3 array, shifted batch of point clouds
    """
    B, N, C = batch_data.shape
    shifts = np.random.uniform(-shift_range, shift_range, (B,3))
    for batch_index in range(B):
        batch_data[batch_index,:,:] += shifts[batch_index,:]
    return batch_data

def jitter_point_cloud(batch_data, sigma=0.01, clip=0.05):
    """ Randomly jitter points. jittering is per point.
        Input:
          BxNx3 array, original batch of point clouds
        Return:
          BxNx3 array, jittered batch of point clouds
    """
    B, N, C = batch_data.shape
    assert(clip > 0)
    jittered_data = np.clip(sigma * np.random.randn(B, N, C), -1*clip, clip)
    jittered_data += batch_data
    return jittered_data

def rotate_perturbation_point_cloud(batch_data, angle_sigma=0.06, angle_clip=0.18):
    """ Randomly perturb the point clouds by small rotations
        Input:
          BxNx3 array, original batch of point clouds
        Return:
          BxNx3 array, rotated batch of point clouds
    """
    rotated_data = np.zeros(batch_data.shape, dtype=np.float32)
    for k in range(batch_data.shape[0]):
        angles = np.clip(angle_sigma*np.random.randn(3), -angle_clip, angle_clip)
        Rx = np.array([[1,0,0],
                       [0,np.cos(angles[0]),-np.sin(angles[0])],
                       [0,np.sin(angles[0]),np.cos(angles[0])]])
        Ry = np.array([[np.cos(angles[1]),0,np.sin(angles[1])],
                       [0,1,0],
                       [-np.sin(angles[1]),0,np.cos(angles[1])]])
        Rz = np.array([[np.cos(angles[2]),-np.sin(angles[2]),0],
                       [np.sin(angles[2]),np.cos(angles[2]),0],
                       [0,0,1]])
        R = np.dot(Rz, np.dot(Ry,Rx))
        shape_pc = batch_data[k, ...]
        rotated_data[k, ...] = np.dot(shape_pc.reshape((-1, 3)), R)
    return rotated_data

import os, sys, h5py

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)

@DATASETS.register_module()
class ModelNet(data.Dataset):
    def __init__(self, config):
        self.root = config.DATA_PATH
        self.npoints = config.npoints
        self.use_normals = config.USE_NORMALS
        self.num_category = config.NUM_CATEGORY
        self.process_data = True
        self.uniform = True
        self.generate_from_raw_data = False
        split = config.subset
        self.subset = config.subset

        if self.num_category == 10:
            self.catfile = os.path.join(self.root, 'modelnet10_shape_names.txt')
        else:
            self.catfile = os.path.join(self.root, 'modelnet40_shape_names.txt')

        self.cat = [line.rstrip() for line in open(self.catfile)]
        self.classes = dict(zip(self.cat, range(len(self.cat))))

        shape_ids = {}
        if self.num_category == 10:
            shape_ids['train'] = [line.rstrip() for line in open(os.path.join(self.root, 'modelnet10_train.txt'))]
            shape_ids['test'] = [line.rstrip() for line in open(os.path.join(self.root, 'modelnet10_test.txt'))]
        else:
            shape_ids['train'] = [line.rstrip() for line in open(os.path.join(self.root, 'modelnet40_train.txt'))]
            shape_ids['test'] = [line.rstrip() for line in open(os.path.join(self.root, 'modelnet40_test.txt'))]

        assert (split == 'train' or split == 'test')
        shape_names = ['_'.join(x.split('_')[0:-1]) for x in shape_ids[split]]
        self.datapath = [(shape_names[i], os.path.join(self.root, shape_names[i], shape_ids[split][i]) + '.txt') for i
                         in range(len(shape_ids[split]))]
        print_log('The size of %s data is %d' % (split, len(self.datapath)), logger='ModelNet')

        if self.uniform:
            self.save_path = os.path.join(self.root,
                                          'modelnet%d_%s_%dpts_fps.dat' % (self.num_category, split, self.npoints))
        else:
            self.save_path = os.path.join(self.root,
                                          'modelnet%d_%s_%dpts.dat' % (self.num_category, split, self.npoints))

        if self.process_data:
            if not os.path.exists(self.save_path):
                # make sure you have raw data in the path before you enable generate_from_raw_data=True.
                if self.generate_from_raw_data:
                    print_log('Processing data %s (only running in the first time)...' % self.save_path, logger='ModelNet')
                    self.list_of_points = [None] * len(self.datapath)
                    self.list_of_labels = [None] * len(self.datapath)

                    for index in tqdm(range(len(self.datapath)), total=len(self.datapath)):
                        fn = self.datapath[index]
                        cls = self.classes[self.datapath[index][0]]
                        cls = np.array([cls]).astype(np.int32)
                        point_set = np.loadtxt(fn[1], delimiter=',').astype(np.float32)

                        if self.uniform:
                            point_set = farthest_point_sample(point_set, self.npoints)
                            print_log("uniformly sampled out {} points".format(self.npoints))
                        else:
                            point_set = point_set[0:self.npoints, :]

                        self.list_of_points[index] = point_set
                        self.list_of_labels[index] = cls

                    with open(self.save_path, 'wb') as f:
                        pickle.dump([self.list_of_points, self.list_of_labels], f)
                else:
                    # no pre-processed dataset found and no raw data found, then load 8192 points dataset then do fps after.
                    self.save_path = os.path.join(self.root,
                                                  'modelnet%d_%s_%dpts_fps.dat' % (
                                                  self.num_category, split, 8192))
                    print_log('Load processed data from %s...' % self.save_path, logger='ModelNet')
                    print_log('since no exact points pre-processed dataset found and no raw data found, load 8192 pointd dataset first, then do fps to {} after, the speed is excepted to be slower due to fps...'.format(self.npoints), logger='ModelNet')
                    with open(self.save_path, 'rb') as f:
                        self.list_of_points, self.list_of_labels = pickle.load(f)

            else:
                print_log('Load processed data from %s...' % self.save_path, logger='ModelNet')
                with open(self.save_path, 'rb') as f:
                    self.list_of_points, self.list_of_labels = pickle.load(f)

        self.shape_names_addr = os.path.join(self.root, 'modelnet40_shape_names.txt')
        with open(self.shape_names_addr) as file:
            lines = file.readlines()
            lines = [line.rstrip() for line in lines]
        self.shape_names = lines

        # TODO: disable for backbones except for PointNEXT!!!
        self.use_height = config.use_height

    def __len__(self):
        return len(self.list_of_labels)

    def _get_item(self, index):
        if self.process_data:
            point_set, label = self.list_of_points[index], self.list_of_labels[index]
        else:
            fn = self.datapath[index]
            cls = self.classes[self.datapath[index][0]]
            label = np.array([cls]).astype(np.int32)
            point_set = np.loadtxt(fn[1], delimiter=',').astype(np.float32)

            if self.uniform:
                point_set = farthest_point_sample(point_set, self.npoints)
            else:
                point_set = point_set[0:self.npoints, :]

        if  self.npoints < point_set.shape[0]:
            point_set = farthest_point_sample(point_set, self.npoints)

        point_set[:, 0:3] = pc_normalize(point_set[:, 0:3])
        if not self.use_normals:
            point_set = point_set[:, 0:3]

        if self.use_height:
            self.gravity_dim = 1
            height_array = point_set[:, self.gravity_dim:self.gravity_dim + 1] - point_set[:,
                                                                            self.gravity_dim:self.gravity_dim + 1].min()
            point_set = np.concatenate((point_set, height_array), axis=1)

        return point_set, label[0]

    def __getitem__(self, index):
        points, label = self._get_item(index)
        pt_idxs = np.arange(0, points.shape[0])  # 2048
        if self.subset == 'train':
            np.random.shuffle(pt_idxs)
        current_points = points[pt_idxs].copy()
        current_points = torch.from_numpy(current_points).float()
        label_name = self.shape_names[int(label)]

        return current_points, label, label_name

@DATASETS.register_module()
class ShapeNet(data.Dataset):
    def __init__(self, config):

        self.data_root = config.DATA_PATH
        self.pc_path = config.PC_PATH
        self.subset = config.subset
        self.npoints = config.npoints
        self.tokenizer = config.tokenizer
        self.train_transform = config.train_transform
        self.id_map_addr = os.path.join(config.DATA_PATH, 'taxonomy.json')
        self.rendered_image_addr = config.IMAGE_PATH
        self.picked_image_type = ['', '_depth0001']
        self.picked_rotation_degrees = list(range(0, 360, 12))
        self.picked_rotation_degrees = [(3 - len(str(degree))) * '0' + str(degree) if len(str(degree)) < 3 else str(degree) for degree in self.picked_rotation_degrees]

        with open(self.id_map_addr, 'r') as f:
            self.id_map = json.load(f)

        self.prompt_template_addr = os.path.join('./data/templates.json')
        with open(self.prompt_template_addr) as f:
            self.templates = json.load(f)[config.pretrain_dataset_prompt]

        self.synset_id_map = {}
        for id_dict in self.id_map:
            synset_id = id_dict["synsetId"]
            self.synset_id_map[synset_id] = id_dict

        self.data_list_file = os.path.join(self.data_root, f'{self.subset}.txt')
        test_data_list_file = os.path.join(self.data_root, 'test.txt')

        self.sample_points_num = self.npoints
        self.whole = config.get('whole')

        print_log(f'[DATASET] sample out {self.sample_points_num} points', logger='ShapeNet-55')
        print_log(f'[DATASET] Open file {self.data_list_file}', logger='ShapeNet-55')
        with open(self.data_list_file, 'r') as f:
            lines = f.readlines()
        if self.whole:
            with open(test_data_list_file, 'r') as f:
                test_lines = f.readlines()
            print_log(f'[DATASET] Open file {test_data_list_file}', logger='ShapeNet-55')
            lines = test_lines + lines
        self.file_list = []
        for line in lines:
            line = line.strip()
            taxonomy_id = line.split('-')[0]
            model_id = line[len(taxonomy_id) + 1:].split('.')[0]
            self.file_list.append({
                'taxonomy_id': taxonomy_id,
                'model_id': model_id,
                'file_path': line
            })
        print_log(f'[DATASET] {len(self.file_list)} instances were loaded', logger='ShapeNet-55')

        self.permutation = np.arange(self.npoints)

        self.uniform = True
        self.augment = True
        self.use_caption_templates = False
        # =================================================
        # TODO: disable for backbones except for PointNEXT!!!
        self.use_height = config.use_height
        # =================================================

        if self.augment:
            print("using augmented point clouds.")

    def pc_norm(self, pc):
        """ pc: NxC, return NxC """
        centroid = np.mean(pc, axis=0)
        pc = pc - centroid
        m = np.max(np.sqrt(np.sum(pc ** 2, axis=1)))
        pc = pc / m
        return pc

    def random_sample(self, pc, num):
        np.random.shuffle(self.permutation)
        pc = pc[self.permutation[:num]]
        return pc

    def __getitem__(self, idx):
        sample = self.file_list[idx]

        data = IO.get(os.path.join(self.pc_path, sample['file_path'])).astype(np.float32)

        if self.uniform and self.sample_points_num < data.shape[0]:
            data = farthest_point_sample(data, self.sample_points_num)
        else:
            data = self.random_sample(data, self.sample_points_num)
        data = self.pc_norm(data)

        if self.augment:
            data = random_point_dropout(data[None, ...])
            data = random_scale_point_cloud(data)
            data = shift_point_cloud(data)
            data = rotate_perturbation_point_cloud(data)
            data = rotate_point_cloud(data)
            data = data.squeeze()

        if self.use_height:
            self.gravity_dim = 1
            height_array = data[:, self.gravity_dim:self.gravity_dim + 1] - data[:,
                                                                       self.gravity_dim:self.gravity_dim + 1].min()
            data = np.concatenate((data, height_array), axis=1)
            data = torch.from_numpy(data).float()
        else:
            data = torch.from_numpy(data).float()

        captions = self.synset_id_map[sample['taxonomy_id']]['name']
        captions = [caption.strip() for caption in captions.split(',') if caption.strip()]
        caption = random.choice(captions)
        captions = []
        tokenized_captions = []
        if self.use_caption_templates:
            for template in self.templates:
                caption = template.format(caption)
                captions.append(caption)
                tokenized_captions.append(self.tokenizer(caption))
        else:
            tokenized_captions.append(self.tokenizer(caption))

        tokenized_captions = torch.stack(tokenized_captions)

        picked_model_rendered_image_addr = self.rendered_image_addr + '/' +\
                                           sample['taxonomy_id'] + '-' + sample['model_id'] + '/'
        picked_image_name = sample['taxonomy_id'] + '-' + sample['model_id'] + '_r_' +\
                            str(random.choice(self.picked_rotation_degrees)) +\
                            random.choice(self.picked_image_type) + '.png'
        picked_image_addr = picked_model_rendered_image_addr + picked_image_name

        try:
            image = pil_loader(picked_image_addr)
            image = self.train_transform(image)
        except:
            raise ValueError("image is corrupted: {}".format(picked_image_addr))

        return sample['taxonomy_id'], sample['model_id'], tokenized_captions, data, image

    def __len__(self):
        return len(self.file_list)


@DATASETS.register_module()
class BuildingNet(data.Dataset):
    def __init__(self, config):

        self.data_root = config.DATA_PATH
        self.pc_path = config.PC_PATH
        self.subset = config.subset
        self.npoints = config.npoints
        self.tokenizer = config.tokenizer
        self.train_transform = config.train_transform
        # self.id_map_addr = os.path.join(config.DATA_PATH, 'taxonomy.json')

        # a list of the model ids: ["COMMERCIALcastle_mesh0365"]
        self.obj_id_addr = os.path.join(config.DATA_PATH, 'train_split.txt')
        self.obj_id_ignore_addr = os.path.join(config.DATA_PATH, 'train_split_ignore.txt')

        # a json dict maps  "COMMERCIALcastle_mesh0365" to ["COMMERCIAL" "castle"]
        self.text_label_addr = os.path.join(config.TEXT_LABEL_PATH, 'train_split_X_classname.json')

        self.rendered_image_addr = config.IMAGE_PATH
        self.picked_image_type = ['', '_depth0001']
        self.picked_rotation_degrees = list(range(0, 360, 12))
        self.picked_rotation_degrees = [(3 - len(str(degree))) * '0' + str(degree) if len(str(degree)) < 3 else str(degree) for degree in self.picked_rotation_degrees]

        # with open(self.id_map_addr, 'r') as f:
        #     self.id_map = json.load(f)

        self.obj_id_ignore = set()
        with open(self.obj_id_ignore_addr, 'r') as f:
            lines = f.readlines()
        for line in lines:
            self.obj_id_ignore.add(line.split('\n')[0])

        print("obj_id_ignore: ", self.obj_id_ignore)

        self.obj_id = list()
        with open(self.obj_id_addr, 'r') as f:
            lines = f.readlines()
        for line in lines:
            building_model_name = line.split('\n')[0]
            if not building_model_name in self.obj_id_ignore:
                self.obj_id.append(building_model_name)
        # print("obj id: ", self.obj_id)


        with open(self.text_label_addr, 'r') as f:
            self.text_label = json.load(f)

        self.prompt_template_addr = os.path.join('./data/templates.json')
        with open(self.prompt_template_addr) as f:
            self.templates = json.load(f)[config.pretrain_dataset_prompt]

        # print("prompt loaded, Yayyyyy!")
        # first_key = next(iter(self.text_label)) 
        # print("first_key: ", first_key)
        # print("loading text classname: ", self.text_label[first_key])


        # self.synset_id_map = {}
        # for id_dict in self.id_map:
        #     synset_id = id_dict["synsetId"]
        #     self.synset_id_map[synset_id] = id_dict

        self.data_list_file = os.path.join(self.data_root, f'{self.subset}_split.txt')
       
        # a txt file of all .npy files - test.txt
        test_data_list_file = os.path.join(self.data_root, 'val_split.txt')

        self.sample_points_num = self.npoints
        self.whole = config.get('whole')

        print_log(f'[DATASET] sample out {self.sample_points_num} points', logger='BuildingNet')
        print_log(f'[DATASET] Open file {self.data_list_file}', logger='BuildingNet')
        with open(self.data_list_file, 'r') as f:
            lines = f.readlines()
        if self.whole:
            with open(test_data_list_file, 'r') as f:
                test_lines = f.readlines()
            print_log(f'[DATASET] Open file {test_data_list_file}', logger='BuildingNet')
            lines = test_lines + lines
        self.file_list = []
        for line in lines:
            line = line.strip()
            model_id = line.split('\n')[0]
            self.file_list.append(model_id)
            # model_id = line[len(taxonomy_id) + 1:].split('.')[0]
        #     self.file_list.append({
        #         'taxonomy_id': taxonomy_id,
        #         'model_id': model_id,
        #         'file_path': line
        #     })
        print_log(f'[DATASET] {len(self.file_list)} instances were loaded', logger='BuildingNet')

        self.permutation = np.arange(self.npoints)

        self.uniform = True
        self.augment = True
        self.use_caption_templates = False
        # =================================================
        # TODO: disable for backbones except for PointNEXT!!!
        self.use_height = config.use_height
        # =================================================

        if self.augment:
            print("using augmented point clouds.")
    
    def load_ply(self, pc_file_path, input_feat="rgb"):
        # filepath = os.path.join(self.data_root, self.data_paths[index])
        filepath = pc_file_path
        plydata = PlyData.read(filepath)
        data = plydata.elements[0].data
        coords = np.array([data["x"], data["y"], data["z"]], dtype=np.float32).T
        feats = np.full((coords.shape[0], 1), -1)
        if input_feat == "rgb":
            feats = np.array([data["red"], data["green"], data["blue"]], dtype=np.float32).T
        elif input_feat == "normals":
            feats = np.array([data["nx"], data["ny"], data["nz"]], dtype=np.float32).T
        elif input_feat == "normals_rgb":
            feats = np.array([data["nx"], data["ny"], data["nz"], data["red"], data["green"], data["blue"]],
                             dtype=np.float32).T
        labels = np.full((coords.shape[0], 1), -1).astype(np.int32)
        vertex_properties = [p.name for p in plydata["vertex"].properties]
        if "label" in vertex_properties:
            labels = np.array(data["label"], dtype=np.int32)
        return coords, feats, labels

    def pc_norm(self, pc):
        """ pc: NxC, return NxC """
        centroid = np.mean(pc, axis=0)
        pc = pc - centroid
        m = np.max(np.sqrt(np.sum(pc ** 2, axis=1)))
        pc = pc / m
        return pc

    def random_sample(self, pc, num):
        np.random.shuffle(self.permutation)
        pc = pc[self.permutation[:num]]
        return pc

    def __getitem__(self, idx):
        # sample = self.file_list[idx]
            
        model_name = self.obj_id[idx]
        model_name_file = model_name + '.ply'
        
        #################################
        # Generate Point Cloud Triplets #
        #################################
        # get pc file
        pc_file_path = os.path.join(self.pc_path, model_name_file)

        coords, feats, labels = self.load_ply(pc_file_path, input_feat="rgb")
        # pc_data = (coords, feats, labels)

        ######################################
        # Convert coords / feat to pc tensor #
        ######################################

        # coords = np.concatenate((coords, feat), 1)
        # pc_data = torch.from_numpy(coords)
        data = coords

        # data = IO.get(os.path.join(self.pc_path, model_name_file)).astype(np.float32)

                #################################################################################
                # Since we are not loading tensors, there's no sample, pc_norm, aug, use_height #
                #################################################################################

        if self.uniform and self.sample_points_num < data.shape[0]:
            data = farthest_point_sample(data, self.sample_points_num)
        else:
            data = self.random_sample(data, self.sample_points_num)
        data = self.pc_norm(data)

        if self.augment:
            data = random_point_dropout(data[None, ...])
            data = random_scale_point_cloud(data)
            data = shift_point_cloud(data)
            data = rotate_perturbation_point_cloud(data)
            data = rotate_point_cloud(data)
            data = data.squeeze()

        if self.use_height:
            self.gravity_dim = 1
            height_array = data[:, self.gravity_dim:self.gravity_dim + 1] - data[:,
                                                                       self.gravity_dim:self.gravity_dim + 1].min()
            data = np.concatenate((data, height_array), axis=1)
            data = torch.from_numpy(data).float()
        else:
            data = torch.from_numpy(data).float()

                #################################################################################
                # Finish preprocessing the tensors#
                #################################################################################
    
        test_text = self.text_label[model_name]
        # print("test_text: ", test_text)
        # print_log(f'[test_text] {test_text}', logger='BuildingNet')
        captions = ','.join(self.text_label[model_name])
        # print_log(f'[captions] {captions}', logger='BuildingNet')
        # captions = self.synset_id_map[sample['taxonomy_id']]['name']
        captions = [caption.strip() for caption in captions.split(',') if caption.strip()]
        caption = random.choice(captions)
        captions = []
        tokenized_captions = []
        if self.use_caption_templates:
            for template in self.templates:
                caption = template.format(caption)
                captions.append(caption)
                tokenized_captions.append(self.tokenizer(caption))
        else:
            tokenized_captions.append(self.tokenizer(caption))

        tokenized_captions = torch.stack(tokenized_captions)
        
        # picked_model_rendered_image_addr = self.rendered_image_addr + '/' +\
        #                                    model_name  + '/OBJ_MODELS/OBJ_MODELS_'
        # picked_image_name = sample['taxonomy_id'] + '-' + sample['model_id'] + '_r_' +\
        #                     str(random.choice(self.picked_rotation_degrees)) +\
        #                     random.choice(self.picked_image_type) + '.png'
        # picked_image_addr = picked_model_rendered_image_addr + picked_image_name

        picked_image_addr = self.rendered_image_addr + '/' +\
                                           model_name  + '/OBJ_MODELS/OBJ_MODELS' + '_r_' +\
                            str(random.choice(self.picked_rotation_degrees)) +\
                            random.choice(self.picked_image_type) + '.png'

        # picked_image_addr = r"{}".format(picked_image_addr)
        # print_log(f'[train_transform: - {type(self.train_transform)}]', logger='BuildingNet')
        try:
            image = pil_loader(picked_image_addr)
            image = self.train_transform(image)
        except:
            # print_log(f'[image is corrupted] {picked_image_addr}', logger='BuildingNet')
            # return model_name, tokenized_captions, None, pc_data
            raise ValueError("image is corrupted: {}".format(picked_image_addr))

        # data is for point cloud (pc_data - numpy)
        return model_name, tokenized_captions, image, data
        # return sample['taxonomy_id'], sample['model_id'], tokenized_captions, data, image

    def __len__(self):
        return len(self.file_list)


import collections.abc as container_abcs
int_classes = int
from torch._six import string_classes

import re
default_collate_err_msg_format = (
    "default_collate: batch must contain tensors, numpy arrays, numbers, "
    "dicts or lists; found {}")
np_str_obj_array_pattern = re.compile(r'[SaUO]')

def customized_collate_fn(batch):
    r"""Puts each data field into a tensor with outer dimension batch size"""

    elem = batch[0]
    elem_type = type(elem)

    if isinstance(batch, list):
        batch = [example for example in batch if example[2] is not None]
        # batch = [example for example in batch]

    if isinstance(elem, torch.Tensor):
        out = None
        if torch.utils.data.get_worker_info() is not None:
            # If we're in a background process, concatenate directly into a
            # shared memory tensor to avoid an extra copy
            numel = sum([x.numel() for x in batch])
            storage = elem.storage()._new_shared(numel)
            out = elem.new(storage)
        # /home/ubuntu/cs231n/ULIP_multi_modal/data/dataset_3d.py:698: UserWarning: An output with one or more elements was resized since it had shape [19200000], which does not match the required output shape [64, 100000, 3]. This behavior is deprecated, and in a future PyTorch release outputs will not be resized unless they have zero elements. You can explicitly reuse an out tensor t by resizing it, inplace, to zero elements with t.resize_(0). (Triggered internally at ../aten/src/ATen/native/Resize.cpp:17.)
        return torch.stack(batch, 0, out=out)
    elif elem_type.__module__ == 'numpy' and elem_type.__name__ != 'str_' \
            and elem_type.__name__ != 'string_':
        if elem_type.__name__ == 'ndarray' or elem_type.__name__ == 'memmap':
            # array of string classes and object
            if np_str_obj_array_pattern.search(elem.dtype.str) is not None:
                raise TypeError(default_collate_err_msg_format.format(elem.dtype))

            return customized_collate_fn([torch.as_tensor(b) for b in batch])
        elif elem.shape == ():  # scalars
            return torch.as_tensor(batch)
    elif isinstance(elem, float):
        return torch.tensor(batch, dtype=torch.float64)
    elif isinstance(elem, int_classes):
        return torch.tensor(batch)
    elif isinstance(elem, string_classes):
        return batch
    elif isinstance(elem, container_abcs.Mapping):
        return {key: customized_collate_fn([d[key] for d in batch]) for key in elem}
    elif isinstance(elem, tuple) and hasattr(elem, '_fields'):  # namedtuple
        return elem_type(*(customized_collate_fn(samples) for samples in zip(*batch)))
    elif isinstance(elem, container_abcs.Sequence):
        # check to make sure that the elements in batch have consistent size
        it = iter(batch)
        elem_size = len(next(it))
        if not all(len(elem) == elem_size for elem in it):
            raise RuntimeError('each element in list of batch should be of equal size')
        transposed = zip(*batch)
        return [customized_collate_fn(samples) for samples in transposed]

    raise TypeError(default_collate_err_msg_format.format(elem_type))


def merge_new_config(config, new_config):
    for key, val in new_config.items():
        if not isinstance(val, dict):
            if key == '_base_':
                with open(new_config['_base_'], 'r') as f:
                    try:
                        val = yaml.load(f, Loader=yaml.FullLoader)
                    except:
                        val = yaml.load(f)
                config[key] = EasyDict()
                merge_new_config(config[key], val)
            else:
                config[key] = val
                continue
        if key not in config:
            config[key] = EasyDict()
        merge_new_config(config[key], val)
    return config

def cfg_from_yaml_file(cfg_file):
    config = EasyDict()
    with open(cfg_file, 'r') as f:
        new_config = yaml.load(f, Loader=yaml.FullLoader)
    merge_new_config(config=config, new_config=new_config)
    return config

class Dataset_3D():
    def __init__(self, args, tokenizer, dataset_type, train_transform=None):
        if dataset_type == 'train':
            self.dataset_name = args.pretrain_dataset_name
        elif dataset_type == 'val':
            self.dataset_name = args.validate_dataset_name
        else:
            raise ValueError("not supported dataset type.")
        with open('./data/dataset_catalog.json', 'r') as f:
            self.dataset_catalog = json.load(f)
            self.dataset_usage = self.dataset_catalog[self.dataset_name]['usage']
            self.dataset_split = self.dataset_catalog[self.dataset_name][self.dataset_usage]
            self.dataset_config_dir = self.dataset_catalog[self.dataset_name]['config']
        self.tokenizer = tokenizer
        self.train_transform = train_transform
        self.pretrain_dataset_prompt = args.pretrain_dataset_prompt
        self.validate_dataset_prompt = args.validate_dataset_prompt
        self.build_3d_dataset(args, self.dataset_config_dir)

    def build_3d_dataset(self, args, config):
        config = cfg_from_yaml_file(config)
        config.tokenizer = self.tokenizer
        config.train_transform = self.train_transform
        config.pretrain_dataset_prompt = self.pretrain_dataset_prompt
        config.validate_dataset_prompt = self.validate_dataset_prompt
        config.args = args
        config.use_height = args.use_height
        config.npoints = args.npoints
        config_others = EasyDict({'subset': self.dataset_split, 'whole': True})
        self.dataset = build_dataset_from_cfg(config, config_others)
