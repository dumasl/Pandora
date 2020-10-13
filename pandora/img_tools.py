#!/usr/bin/env python
# coding: utf8
#
# Copyright (c) 2020 Centre National d'Etudes Spatiales (CNES).
#
# This file is part of PANDORA
#
#     https://github.com/CNES/Pandora_pandora
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""
This module contains functions associated to raster images.
"""

import logging
from typing import Dict, List, Union, Tuple

import cv2
import numpy as np
import rasterio
import xarray as xr
from scipy.ndimage.interpolation import zoom


def read_img(img: str, no_data: float, cfg: Dict, mask: xr.Dataset = None) -> xr.Dataset:
    """
    Read image and mask, and return the corresponding xarray.DataSet

    :param img: Path to the image
    :type img: string
    :type no_data: no_data value in the image
    :type no_data: float
    :param cfg: image configuration, containing the mask conventions generated by this function
    :param cfg: dict
    :param mask: Path to the mask (optional)
    :type mask: string
    :return: xarray.DataSet
    :rtype:
        xarray.DataSet containing the variables :
            - im : 2D (row, col) xarray.DataArray float32
            - msk : 2D (row, col) xarray.DataArray int16, with the convention defined in the configuration file
    """
    img_ds = rasterio.open(img)
    data = img_ds.read(1)
    dataset = xr.Dataset({'im': (['row', 'col'], data.astype(np.float32))},
                         coords={'row': np.arange(data.shape[0]),
                                 'col': np.arange(data.shape[1])})
    # Add image conf to the image dataset
    dataset.attrs = {'no_data_img': no_data,
                     'valid_pixels': cfg["valid_pixels"],
                     'no_data_mask': cfg["no_data"]}
    # If there is no mask, and no data in the images, do not create the mask to minimize calculation time
    no_data_pixels = np.where(data == no_data)
    if mask is None and no_data_pixels[0].size == 0:
        return dataset

    # Allocate the mask
    dataset['msk'] = xr.DataArray(np.full((data.shape[0], data.shape[1]), int(cfg['valid_pixels'])).astype(np.int16),
                                  dims=['row', 'col'])

    # Mask invalid pixels if needed
    if mask is not None:
        input_mask = rasterio.open(mask).read(1)
        # Masks invalid pixels
        # All pixels that are not no_data and valid_pixels are considered as invalid pixels
        dataset['msk'].data[np.where(input_mask > 0)] = int(cfg['valid_pixels']) + int(cfg['no_data']) + 1

    # Masks no_data pixels
    # If a pixel is invalid due to the input mask, and it is also no_data, then the value of this pixel in the
    # generaceted mask will be = no_data
    dataset['msk'].data[no_data_pixels] = int(cfg['no_data'])

    return dataset


def prepare_pyramid(img_left: xr.Dataset, img_right: xr.Dataset, num_scales: int, scale_factor: int) -> \
        Tuple[List[xr.Dataset], List[xr.Dataset]]:
    """
    Return a List with the datasets at the different scales

    :param img_left: left Dataset image
    :type img_left:
    xarray.Dataset containing :
        - im : 2D (row, col) xarray.DataArray
    :param img_right: right Dataset image
    :type img_right:
    xarray.Dataset containing :
        - im : 2D (row, col) xarray.DataArray
    :param num_scales: number of scales
    :type num_scales: int
    :param scale_factor: factor by which downsample the images
    :type scale_factor: int
    :return: a List that contains the different scaled datasets
    :rtype : List of xarray.Dataset
    """

    # Create multiscale pyramid.
    pyramid_left = []
    pyramid_right = []

    pyramid_left.append(img_left)
    pyramid_right.append(img_right)

    scales = np.arange(num_scales - 1)
    for scale in scales:
        # Downscale the previous layer
        pyramid_left.append(create_downsampled_dataset(pyramid_left[-1], scale_factor))
        pyramid_right.append(create_downsampled_dataset(pyramid_right[-1], scale_factor))

    # The pyramid is intended to be from coarse to original size, so we inverse its order.
    return pyramid_left[::-1], pyramid_right[::-1]


def create_downsampled_dataset(img_orig: xr.Dataset, scale_factor: int) -> xr.Dataset:
    """
    Return a xr.Dataset downsampled dataset

    :param img_orig : original Dataset image
    :type img_orig:
    xarray.Dataset containing :
        - im : 2D (row, col) xarray.DataArray

    :param scale_factor: factor by which downsample the image
    :type scale_factor: int
    :return: a downsampled dataset
    :rtype : array of xarray.Dataset
    """

    # Downsampling and appliying gaussian kernel to original image
    if scale_factor == 1:
        return img_orig

    img = cv2.GaussianBlur(img_orig['im'].data, ksize=(5, 5), sigmaX=1.2, sigmaY=1.2)
    img_downs = cv2.resize(img, dsize=(
        int(img_orig['im'].data.shape[1] / scale_factor), int(img_orig['im'].data.shape[0] / scale_factor)),
                           interpolation=cv2.INTER_AREA)

    # Downsampling mask if exists, otherwise create mask of all valid
    if 'msk' in img_orig:
        mask_downs = cv2.resize(img, dsize=(
            int(img_orig['msk'].data.shape[1] / scale_factor), int(img_orig['msk'].data.shape[0] / scale_factor)),
                                interpolation=cv2.INTER_AREA)
    else:
        mask_downs = img_downs * 0
    # Creating new dataset
    dataset = xr.Dataset({'im': (['row', 'col'], img_downs.astype(np.float32))},
                         coords={'row': np.arange(img_downs.shape[0]),
                                 'col': np.arange(img_downs.shape[1])})

    # Allocate the mask
    dataset['msk'] = xr.DataArray(np.full((img_downs.shape[0], img_downs.shape[1]), mask_downs.astype(np.int16)),
                                  dims=['row', 'col'])

    # Add image conf to the image dataset
    dataset.attrs = {'no_data_img': img_orig.attrs['no_data_img'],
                     'valid_pixels': img_orig.attrs["valid_pixels"],
                     'no_data_mask': img_orig.attrs["no_data_mask"]}
    return dataset


def shift_right_img(img_right: xr.Dataset, subpix: int) -> List[xr.Dataset]:
    """
    Return an array that contains the shifted right images

    :param img_right: right Dataset image
    :type img_right:
    xarray.Dataset containing :
        - im : 2D (row, col) xarray.DataArray

    :param subpix: subpixel precision = (1 or 2 or 4)
    :type subpix: int
    :return: an array that contains the shifted right images
    :rtype : array of xarray.Dataset
    """
    img_right_shift = [img_right]
    ny_, nx_ = img_right['im'].shape

    # zoom factor = (number of columns with zoom / number of original columns)
    if subpix == 2:
        # Shift the right image for subpixel precision 0.5
        data = zoom(img_right['im'].data, (1, (nx_ * 4 - 3) / float(nx_)), order=1)[:, 2::4]
        col = np.arange(img_right.coords['col'][0] + 0.5, img_right.coords['col'][-1], step=1)
        img_right_shift.append(xr.Dataset({'im': (['row', 'col'], data)},
                                          coords={'row': np.arange(ny_), 'col': col}))

    if subpix == 4:
        # Shift the right image for subpixel precision 0.25
        data = zoom(img_right['im'].data, (1, (nx_ * 4 - 3) / float(nx_)), order=1)[:, 1::4]
        col = np.arange(img_right.coords['col'][0] + 0.25, img_right.coords['col'][-1], step=1)
        img_right_shift.append(xr.Dataset({'im': (['row', 'col'], data)},
                                          coords={'row': np.arange(ny_), 'col': col}))

        # Shift the right image for subpixel precision 0.5
        data = zoom(img_right['im'].data, (1, (nx_ * 4 - 3) / float(nx_)), order=1)[:, 2::4]
        col = np.arange(img_right.coords['col'][0] + 0.5, img_right.coords['col'][-1], step=1)
        img_right_shift.append(xr.Dataset({'im': (['row', 'col'], data)},
                                          coords={'row': np.arange(ny_), 'col': col}))

        # Shift the right image for subpixel precision 0.75
        data = zoom(img_right['im'].data, (1, (nx_ * 4 - 3) / float(nx_)), order=1)[:, 3::4]
        col = np.arange(img_right.coords['col'][0] + 0.75, img_right.coords['col'][-1], step=1)
        img_right_shift.append(xr.Dataset({'im': (['row', 'col'], data)},
                                          coords={'row': np.arange(ny_), 'col': col}))
    return img_right_shift


def check_inside_image(img: xr.Dataset, x: int, y: int) -> bool:
    """
    Check if the coordinates x,y are inside the image

    :param img: Dataset image
    :type img:
        xarray.Dataset containing :
            - im : 2D (row, col) xarray.DataArray
    :param x: row coordinates
    :type x: int
    :param y: column coordinates
    :type y: int
    :return: a boolean
    :rtype: boolean
    """
    nx_, ny_ = img['im'].shape
    return 0 <= x < nx_ and 0 <= y < ny_


def census_transform(image: xr.Dataset, window_size: int) -> xr.Dataset:
    """
    Generates the census transformed image from an image

    :param image: Dataset image
    :type image: xarray.Dataset containing the image im : 2D (row, col) xarray.Dataset
    :param window_size: Census window size
    :type window_size: int
    :return: Dataset census transformed uint32
    :rtype: xarray.Dataset containing the transformed image im: 2D (row, col) xarray.DataArray uint32
    """
    ny_, nx_ = image['im'].shape
    border = int((window_size - 1) / 2)

    # Create a sliding window of using as_strided function : this function create a new a view (by manipulating data
    #  pointer) of the image array with a different shape. The new view pointing to the same memory block as
    # image so it does not consume any additional memory.
    str_row, str_col = image['im'].data.strides
    shape_windows = (ny_ - (window_size - 1), nx_ - (window_size - 1), window_size, window_size)
    strides_windows = (str_row, str_col, str_row, str_col)
    windows = np.lib.stride_tricks.as_strided(image['im'].data, shape_windows, strides_windows, writeable=False)

    # Pixels inside the image which can be centers of windows
    central_pixels = image['im'].data[border:-border, border:-border]

    # Allocate the census mask
    census = np.zeros((ny_ - (window_size - 1), nx_ - (window_size - 1)), dtype='uint32')

    shift = (window_size * window_size) - 1
    for row in range(window_size):
        for col in range(window_size):
            # Computes the difference and shift the result
            census[:, :] += ((windows[:, :, row, col] > central_pixels[:, :]) << shift).astype(np.uint32)
            shift -= 1

    census = xr.Dataset({'im': (['row', 'col'], census)},
                        coords={'row': np.arange(border, ny_ - border),
                                'col': np.arange(border, nx_ - border)})

    return census


def compute_mean_raster(img: xr.Dataset, win_size: int) -> np.ndarray:
    """
    Compute the mean within a sliding window for the whole image

    :param img: Dataset image
    :type img:
        xarray.Dataset containing :
            - im : 2D (row, col) xarray.DataArray
    :param win_size: window size
    :type win_size: int
    :return: mean raster
    :rtype: numpy array
    """
    ny_, nx_ = img['im'].shape

    # Example with win_size = 3 and the input :
    #           10 | 5  | 3
    #            2 | 10 | 5
    #            5 | 3  | 1

    # Compute the cumulative sum of the elements along the column axis
    r_mean = np.r_[np.zeros((1, nx_)), img['im']]
    # r_mean :   0 | 0  | 0
    #           10 | 5  | 3
    #            2 | 10 | 5
    #            5 | 3  | 1
    r_mean = np.cumsum(r_mean, axis=0)
    # r_mean :   0 | 0  | 0
    #           10 | 5  | 3
    #           12 | 15 | 8
    #           17 | 18 | 9
    r_mean = r_mean[win_size:, :] - r_mean[:-win_size, :]
    # r_mean :  17 | 18 | 9

    # Compute the cumulative sum of the elements along the row axis
    r_mean = np.c_[np.zeros(ny_ - (win_size - 1)), r_mean]
    # r_mean :   0 | 17 | 18 | 9
    r_mean = np.cumsum(r_mean, axis=1)
    # r_mean :   0 | 17 | 35 | 44
    r_mean = r_mean[:, win_size:] - r_mean[:, :-win_size]
    # r_mean : 44
    return r_mean / float(win_size * win_size)


def compute_mean_patch(img: xr.Dataset, x: int, y: int, win_size: int) -> np.ndarray:
    """
    Compute the mean within a window centered at position x,y

    :param img: Dataset image
    :type img:
        xarray.Dataset containing :
            - im : 2D (row, col) xarray.DataArray
    :param x: row coordinates
    :type x: int
    :param y: column coordinates
    :type y: int
    :param win_size: window size
    :type win_size: int
    :return: mean
    :rtype : float
    """
    begin_window = (x - int(win_size / 2), y - int(win_size / 2))
    end_window = (x + int(win_size / 2), y + int(win_size / 2))

    # Check if the window is inside the image, and compute the mean
    if check_inside_image(img, begin_window[0], begin_window[1]) and \
            check_inside_image(img, end_window[0], end_window[1]):
        return np.mean(img['im'][begin_window[1]:end_window[1] + 1, begin_window[0]:end_window[0] + 1],
                       dtype=np.float32)

    logging.error("The window is outside the image")
    raise IndexError


def compute_std_raster(img: xr.Dataset, win_size: int) -> np.ndarray:
    """
    Compute the standard deviation within a sliding window for the whole image
    with the formula : std = sqrt( E[x^2] - E[x]^2 )

    :param img: Dataset image
    :type img:
        xarray.Dataset containing :
            - im : 2D (row, col) xarray.DataArray
    :param win_size: window size
    :type win_size: int
    :return: std raster
    :rtype : numpy array
    """
    # Computes E[x]
    mean_ = compute_mean_raster(img, win_size)

    # Computes E[x^2]
    raster_power_two = xr.Dataset({'im': (['row', 'col'], img['im'].data ** 2)},
                                  coords={'row': np.arange(img['im'].shape[0]), 'col': np.arange(img['im'].shape[1])})
    mean_power_two = compute_mean_raster(raster_power_two, win_size)

    # Compute sqrt( E[x^2] - E[x]^2 )
    var = mean_power_two - mean_ ** 2
    # Avoid very small values
    var[np.where(var < (10 ** (-15) * abs(mean_power_two)))] = 0
    return np.sqrt(var)


def read_disp(disparity: Union[None, int, str]) -> Union[None, int, np.ndarray]:
    """
    Read the disparity :
        - if cfg_disp is the path of a disparity grid, read and return the grid (type numpy array)
        - else return the value of cfg_disp

    :param disparity: disparity, or path to the disparity grid
    :type disparity: None, int or str
    :return: the disparity
    :rtype: int or np.ndarray
    """
    if type(disparity) == str:
        disp_ = rasterio.open(disparity)
        data_disp = disp_.read(1)
    else:
        data_disp = disparity

    return data_disp
