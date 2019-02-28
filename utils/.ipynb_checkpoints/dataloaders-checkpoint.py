import sys
import os, csv, random, math, json
import glob

import rasterio
import cv2

from multiprocessing import Pool

from PIL import Image
import numpy as np
import pandas as pd

import skimage.io
from scipy.ndimage import zoom
from skimage.transform import resize

import torch
import torch.utils.data as data
from torch.autograd import Variable

from torchvision.transforms import functional

band_ids = ['B01', 'B02', 'B03', 'B04', 'B05', 'B06', 'B07', 'B08', 'B8A', 'B09', 'B10', 'B11', 'B12']
band_maxs = {'B01':19348, 'B02':20566, 'B03':18989, 'B04':17881,
                'B05':17374, 'B06':17160, 'B07':16950, 'B08':16708,
                'B8A':16627, 'B09':16204, 'B10': 6000, 'B11':15465, 'B12':15273}

def read_band(band):
    r = rasterio.open(band)
    data = r.read()[0]
    r.close()
    return data 

def read_bands(band_paths):
    pool = Pool(26)
    bands = pool.map(read_band, band_paths)
    pool.close()
    return bands

def _match_band(two_date):
    return match_band(two_date[1], two_date[0])

def match_bands(date1, date2):
    pool = Pool(13)
    date2 = pool.map(_match_band, [[date1[i], date2[i]] for i in range(len(date1))])
    pool.close()
    return date2

def _resize(band):
    return cv2.resize(band, (10980, 10980))

def stack_bands(bands):
    pool = Pool(26)
    bands = pool.map(_resize, bands)
    pool.close()
    pool = Pool(26)
    bands = pool.map(stretch_8bit, bands)
    pool.close()

    return np.stack(bands[:13]).astype(np.float32), np.stack(bands[13:]).astype(np.float32)

def match_band(source, template):
    """
    Adjust the pixel values of a grayscale image such that its histogram
    matches that of a target image
    Arguments:
    -----------
        source: np.ndarray
            Image to transform; the histogram is computed over the flattened
            array
        template: np.ndarray
            Template image; can have different dimensions to source
    Returns:
    -----------
        matched: np.ndarray
            The transformed output image
    """
    oldshape = source.shape
    source = source.ravel()
    template = template.ravel()

    perm = source.argsort(kind='heapsort')

    aux = source[perm]
    flag = np.concatenate(([True], aux[1:] != aux[:-1]))
    s_values = aux[flag]

    iflag = np.cumsum(flag) - 1
    inv_idx = np.empty(source.shape, dtype=np.intp)
    inv_idx[perm] = iflag
    bin_idx = inv_idx

    idx = np.concatenate(np.nonzero(flag) + ([source.size],))
    s_counts = np.diff(idx)

    a = pd.value_counts(template).sort_index()
    t_values = np.asarray(a.index)
    t_counts = np.asarray(a.values)

    s_quantiles = np.cumsum(s_counts).astype(np.float32)
    s_quantiles /= s_quantiles[-1]

    t_quantiles = np.cumsum(t_counts).astype(np.float32)
    t_quantiles /= t_quantiles[-1]

    # interpolate linearly to find the pixel values in the template image
    # that correspond most closely to the quantiles in the source image
    interp_t_values = np.interp(s_quantiles, t_quantiles, t_values)

    return interp_t_values[bin_idx].reshape(oldshape)


def stretch_8bit(band, d):
    a = 0
    b = 255
    c = 0
    t = a + (band - c) * ((b - a) / (d - c))
    t[t<a] = a
    t[t>b] = b
    return t.astype(np.uint8)


def get_train_val_metadata(data_dir, val_cities, patch_size, stride):
    cities = os.listdir(data_dir + 'train_labels/')
    cities.sort()
    val_cities = list(map(int, val_cities.split(',')))
    train_cities = list(set(range(len(cities))).difference(val_cities))

    train_metadata = []
    for city_no in train_cities:
        city_label = cv2.imread(data_dir + 'train_labels/' + cities[city_no] + '/cm/cm.png', 0) / 255

        for i in range(0, city_label.shape[0], stride):
            for j in range(0, city_label.shape[1], stride):
                if (i + patch_size) <= city_label.shape[0] and (j + patch_size) <= city_label.shape[1]:
                    train_metadata.append([cities[city_no], i, j])

    val_metadata = []
    for city_no in val_cities:
        city_label = cv2.imread(data_dir + 'train_labels/' + cities[city_no] + '/cm/cm.png', 0) / 255
        for i in range(0, city_label.shape[0], patch_size):
            for j in range(0, city_label.shape[1], patch_size):
                if (i + patch_size) <= city_label.shape[0] and (j + patch_size) <= city_label.shape[1]:
                    val_metadata.append([cities[city_no], i, j])

    return train_metadata, val_metadata

def city_loader(city_path_meta):
    city_path = city_path_meta[0]
    h = city_path_meta[1]
    w = city_path_meta[2]
    
    base_path1 = glob.glob(city_path + '/imgs_1/*.tif')[0][:-7]
    base_path2 = glob.glob(city_path + '/imgs_2/*.tif')[0][:-7]
    

    bands1_stack = []
    bands2_stack = []
    for band_id in band_ids:
        band1_r = rasterio.open(base_path1 + band_id + '.tif')
        band2_r = rasterio.open(base_path2 + band_id + '.tif')

        band1_d = band1_r.read()[0]
        band2_d = band2_r.read()[0]

        band1_d[band1_d > band_maxs[band_id]] = band_maxs[band_id]
        band2_d[band2_d > band_maxs[band_id]] = band_maxs[band_id]
                
        band2_d = match_band(band2_d, band1_d)

        band1_d = stretch_8bit(band1_d, band_maxs[band_id]).astype(np.float32) / 255.
        band1_d = cv2.resize(band1_d, (h, w))
        bands1_stack.append(band1_d)

        band2_d = stretch_8bit(band2_d, band_maxs[band_id]).astype(np.float32) / 255.
        band2_d = cv2.resize(band2_d, (h, w))
        bands2_stack.append(band2_d)

    two_dates = np.asarray([bands1_stack, bands2_stack])
    two_dates = np.transpose(two_dates, (1,0,2,3))
    
    return two_dates

def label_loader(label_path):
    label = cv2.imread(label_path + '/cm/' + 'cm.png', 0) / 255
    return label

def full_onera_loader(path):
    cities = os.listdir(path + 'train_labels/')

    
    label_paths = []
    for city in cities:
        if '.txt' not in city:
            label_paths.append(path + 'train_labels/' + city)
    
    pool = Pool(len(label_paths))
    city_labels = pool.map(label_loader, label_paths)
    
    city_paths_meta = []
    i = 0
    for city in cities:
        if '.txt' not in city:
            city_paths_meta.append([path + 'images/' + city, city_labels[i].shape[1], city_labels[i].shape[0]])
            i += 1
            
    city_loads = pool.map(city_loader, city_paths_meta)
    pool.close()
    
    dataset = {}
    for cp in range(len(label_paths)):
        city = label_paths[cp].split('/')[-1]
        dataset[city] = {'images':city_loads[cp] , 'labels': city_labels[cp].astype(np.uint8)}

    return dataset

def full_onera_multidate_loader(path, bands):
    fin = open(path + 'multidate_metadata.json','r')
    metadata = json.load(fin)
    fin.close()

    cities = os.listdir(path + 'train_labels/')

    dataset = {}
    for city in cities:
        if city in metadata:
            dates_stack = []
            label = cv2.imread(path + 'train_labels/' + city + '/cm/' + 'cm.png', 0) / 255

            first_date = True
            for date_no in range(5):
                bands_stack = []
                base_path = glob.glob(metadata[city][str(date_no)] + '/*.tif')[0][:-7]

                for band_no in range(len(bands)):
                    band_r = rasterio.open(base_path + bands[band_no] + '.tif')
                    band_d = band_r.read()[0]

                    if not first_date:
                        band_d = match_band(band_d, dates_stack[0][band_no])

                    band_d = stretch_8bit(band_d, 2, 98).astype(np.float32) / 255.
                    band_d = cv2.resize(band_d, (label.shape[1], label.shape[0]))
                    bands_stack.append(band_d)

                if not first_date:
                    first_date = False

                dates_stack.append(bands_stack)

            dates_stack = np.asarray(dates_stack).transpose(1,0,2,3)
            dataset[city] = {'images':dates_stack , 'labels': label.astype(np.uint8)}

    return dataset

def full_buildings_loader(path):
    dates = os.listdir(path + 'Images/')
    dates.sort()

    label_r = rasterio.open(path + 'Ground_truth/Changes/Changes_06_11.tif')
    label = label_r.read()[0]

    stacked_dates = []
    for date in dates:
        r = rasterio.open(path + 'Images/' + date)
        d = r.read()
        bands = []
        if d.shape[0] == 4:
            for b in d:
                band = stretch_8bit(b, 0.01, 99)
                band = cv2.resize(band, (label.shape[1], label.shape[0]))
                bands.append(band / 255.)
        if d.shape[0] == 8:
            for b in [1,2,4,7]:
                band = stretch_8bit(d[b], 0.01, 99)
                band = cv2.resize(band, (label.shape[1], label.shape[0]))
                bands.append(band/ 255.)
        stacked_dates.append(bands)

    stacked_dates = np.asarray(stacked_dates).astype(np.float32).transpose(1,0,2,3)

    print (stacked_dates.shape, label.shape)
    return {'images':stacked_dates, 'labels':label.astype(np.uint8)}


def onera_loader(dataset, city, x, y, size, aug):
    out_img = dataset[city]['images'][:, : ,x:x+size, y:y+size]
    if aug:
        out_img = np.rot90(out_img, random.randint(0,3), [2,3])
        if random.random() > 0.5:
            out_img = np.flip(out_img, axis=2)
        if random.random() > 0.5:
            out_img = np.flip(out_img, axis=2)

    return out_img, dataset[city]['labels'][x:x+size, y:y+size]

def onera_siamese_loader(dataset, city, x, y, size, aug):
    out_img = np.copy(dataset[city]['images'][:, : ,x:x+size, y:y+size])
    out_lbl = np.copy(dataset[city]['labels'][x:x+size, y:y+size])
    if aug:
        rot_deg = random.randint(0,3)
        out_img = np.rot90(out_img, rot_deg, [2,3]).copy()
        out_lbl = np.rot90(out_lbl, rot_deg, [0,1]).copy()
        
        if random.random() > 0.5:
            out_img = np.flip(out_img, axis=2).copy()
            out_lbl = np.flip(out_lbl, axis=0).copy()
            
        if random.random() > 0.5:
            out_img = np.flip(out_img, axis=3).copy()
            out_lbl = np.flip(out_lbl, axis=1).copy()
            
    out_img = np.transpose(out_img, (1,0,2,3))
    return out_img[0], out_img[1], out_lbl

def buildings_loader(dataset, x, y, size):
    return dataset['images'][:,:, x:x+size, y:y+size], dataset['labels'][x:x+size, y:y+size]

class OneraPreloader(data.Dataset):

    def __init__(self, root, metadata, full_load):
        random.shuffle(metadata)

        self.full_load = full_load
        self.root = root
        self.imgs = metadata
        self.loader = onera_siamese_loader
        self.aug = False
        self.input_size = 120

    def __getitem__(self, index):
        """
        Args:
            index (int): Index

        Returns:
            tuple: (image, target) where target is class_index of the target class.
        """
        city, x, y = self.imgs[index]

        return self.loader(self.full_load, city, x, y, self.input_size, self.aug)

    def __len__(self):
        return len(self.imgs)

class BuildingsPreloader(data.Dataset):

    def __init__(self, root, csv_file, input_size, full_load, loader=buildings_loader):

        r = csv.reader(open(csv_file, 'r'), delimiter=',')

        images_list = []

        for row in r:
            images_list.append([int(row[0]), int(row[1])])

        random.shuffle(images_list)

        self.full_load = full_load
        self.input_size = input_size
        self.root = root
        self.imgs = images_list
        self.loader = loader

    def __getitem__(self, index):
        """
        Args:
            index (int): Index

        Returns:
            tuple: (image, target) where target is class_index of the target class.
        """
        x, y = self.imgs[index]

        img, target = self.loader(self.full_load, x, y, self.input_size)
#         print (img.shape)
        return img, target

    def __len__(self):
        return len(self.imgs)
