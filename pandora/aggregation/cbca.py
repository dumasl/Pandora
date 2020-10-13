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
This module contains functions associated to the Cross Based Cost Aggregation (cbca) method.
"""

from typing import Dict, Union, Tuple, List

import numpy as np
import xarray as xr
from json_checker import Checker, And
from numba import njit
from pandora.filter import AbstractFilter
from pandora.img_tools import shift_right_img

from . import aggregation


@aggregation.AbstractAggregation.register_subclass('cbca')
class CrossBasedCostAggregation(aggregation.AbstractAggregation):
    """
    CrossBasedCostAggregation class, allows to perform the aggregation step
    """
    # Default configuration, do not change these values
    _CBCA_INTENSITY = 30.
    _CBCA_DISTANCE = 5

    def __init__(self, **cfg: dict):
        """
        :param cfg: optional configuration, {'cbca_intensity': value, 'cbca_distance': value}
        :type cfg: dict
        """
        self.cfg = self.check_conf(**cfg)
        self._cbca_intensity = self.cfg['cbca_intensity']
        self._cbca_distance = self.cfg['cbca_distance']

    def check_conf(self, **cfg: Union[str, float, int]) -> Dict[str, Union[str, float, int]]:
        """
        Add default values to the dictionary if there are missing elements and check if the dictionary is correct

        :param cfg: aggregation configuration
        :type cfg: dict
        :return cfg: aggregation configuration updated
        :rtype: dict
        """
        # Give the default value if the required element is not in the configuration
        if 'cbca_intensity' not in cfg:
            cfg['cbca_intensity'] = self._CBCA_INTENSITY
        if 'cbca_distance' not in cfg:
            cfg['cbca_distance'] = self._CBCA_DISTANCE

        schema = {
            "aggregation_method": And(str, lambda x: 'cbca'),
            "cbca_intensity": And(float, lambda x: x > 0),
            "cbca_distance": And(int, lambda x: x > 0)
        }

        checker = Checker(schema)
        checker.validate(cfg)
        return cfg

    def desc(self):
        """
        Describes the aggregation method
        """
        print('CrossBasedCostAggregation method')

    def cost_volume_aggregation(self, img_left: xr.Dataset, img_right: xr.Dataset, cv: xr.Dataset,
                                **cfg: Union[str, int]) -> None:
        """
        Aggregated the cost volume with Cross-Based Cost Aggregation, using the pipeline define in
        Zhang, K., Lu, J., & Lafruit, G. (2009).
        Cross-based local stereo matching using orthogonal integral images.
        IEEE transactions on circuits and systems for video technology, 19(7), 1073-1079.

        :param img_left: left Dataset image
        :type img_left:
            xarray.Dataset containing :
                - im : 2D (row, col) xarray.DataArray
                - msk (optional): 2D (row, col) xarray.DataArray
        :param img_right: right Dataset image
        :type img_right:
            xarray.Dataset containing :
                - im : 2D (row, col) xarray.DataArray
                - msk (optional): 2D (row, col) xarray.DataArray
        :param cv: cost volume dataset
        :type cv:
            xarray.Dataset, with the data variables:
                - cost_volume 3D xarray.DataArray (row, col, disp)
                - confidence_measure 3D xarray.DataArray (row, col, indicator)
        :param cfg: images configuration containing the mask convention : valid_pixels, no_data
        :type cfg: dict
        :return: None
        """
        cross_left, cross_right = self.computes_cross_supports(img_left, img_right, cv)

        ny_, nx_, nb_disp = cv['cost_volume'].shape

        # Allocate the numpy aggregated cost volume cv = (disp, col, row), for efficient memory management
        agg = np.zeros((nb_disp, nx_, ny_), dtype=np.float32)

        # Add invalid costs (i.e = np.nan ) to the output aggregated cost volume (because the step 1 of cbca do not
        # propagate invalid pixels, we need to retrieve them at the end of aggregation )
        # Much faster than :
        # id_nan = np.isnan(cv['cost_volume'].data)
        # compute the aggregation ..
        # cv['cost_volume'].data[id_nan] = np.nan
        agg += np.swapaxes(cv['cost_volume'].data, 0, 2)
        agg *= 0

        disparity_range = cv.coords['disp'].data
        range_col = np.arange(0, nx_)

        for d in range(nb_disp):
            i_right = int((disparity_range[d] % 1) * cv.attrs['subpixel'])

            # Step 1 : horizontal integral image
            step1 = cbca_step_1(cv['cost_volume'].data[:, :, d])

            range_col_right = range_col + disparity_range[d]
            valid_index = np.where((range_col_right >= 0) & (range_col_right < cross_right[i_right].shape[1]))

            # Step 2 : horizontal matching cost
            step2, sum2 = cbca_step_2(step1, cross_left, cross_right[i_right], range_col[valid_index],
                                      range_col_right[valid_index].astype(int))

            # Step 3 : vertical integral image
            step3 = cbca_step_3(step2)

            # Step 4 : aggregate cost volume
            step4, sum4 = cbca_step_4(step3, sum2, cross_left, cross_right[i_right], range_col[valid_index],
                                      range_col_right[valid_index].astype(int))

            # Added the pixel anchor pixel to the number of support pixels used during the aggregation
            sum4 += 1
            # Add the aggregate cost to the output
            agg[d, :, :] += np.swapaxes(step4, 0, 1)
            # Normalize the aggregated cost
            agg[d, :, :] /= np.swapaxes(sum4, 0, 1)

        cv['cost_volume'].data = np.swapaxes(agg, 0, 2)
        cv.attrs['aggregation'] = 'cbca'

        # Maximal cost of the cost volume after agregation
        cmax = cv.attrs['cmax'] * ((self._cbca_distance * 2) - 1) ** 2
        cv.attrs['cmax'] = cmax

    def computes_cross_supports(self, img_left: xr.Dataset, img_right: xr.Dataset, cv: xr.Dataset) -> \
            Tuple[np.ndarray, List[np.ndarray]]:
        """
        Prepare images and compute the cross support region of the left and right images.
        A 3x3 median filter is applied to the images before calculating the cross support region.

        :param img_left: left Dataset image
        :type img_left:
            xarray.Dataset containing :
                - im : 2D (row, col) xarray.DataArray
                - msk (optional): 2D (row, col) xarray.DataArray
        :param img_right: right Dataset image
        :type img_right:
            xarray.Dataset containing :
                - im : 2D (row, col) xarray.DataArray
                - msk (optional): 2D (row, col) xarray.DataArray
        :param cv: cost volume dataset
        :type cv:
            xarray.Dataset, with the data variables:
                - cost_volume 3D xarray.DataArray (row, col, disp)
                - confidence_measure 3D xarray.DataArray (row, col, indicator)
        :return: the left and right cross support region
        :rtype: Tuples(left cross support region, List(right cross support region))
        """
        subpix = cv.attrs['subpixel']
        offset = int(cv.attrs['offset_row_col'])

        # shift the right image
        img_right_shift = shift_right_img(img_right, subpix)

        # Median filter on valid pixels
        filter_ = AbstractFilter(**{'filter_method': 'median', 'filter_size': 3})

        # Invalid and no data pixels are masked with np.nan to avoid propagating the values with the median filter
        left_masked = np.copy(img_left['im'].data)
        if 'msk' in img_left.data_vars:
            left_masked[np.where(img_left['msk'].data != img_left.attrs['valid_pixels'])] = np.nan

        left_masked = filter_.median_filter(left_masked)
        # Convert nan to inf to be able to use the comparison operators < and > in cross_support function
        np.nan_to_num(left_masked, copy=False, nan=np.inf)
        # Compute left cross support using numba to reduce running time
        if offset != 0:
            # Cross support to the size of the cost volume
            cross_left = cross_support(left_masked[offset: -offset, offset: -offset], self._cbca_distance,
                                       self._cbca_intensity)
        else:
            cross_left = cross_support(left_masked, self._cbca_distance, self._cbca_intensity)

        # Compute the right cross support. Apply a 3×3 median filter to the input image
        cross_right = []
        for shift in range(0, len(img_right_shift)):
            # Invalid and nodata pixels are masked with np.nan to avoid propagating the values with the median filter
            right_masked = np.copy(img_right_shift[shift]['im'].data)

            # Pixel precision
            if ('msk' in img_right.data_vars) and (shift == 0):
                right_masked[np.where(img_right['msk'].data != img_right.attrs['valid_pixels'])] = np.nan

            # Subpixel precision : computes the shifted right mask
            if ('msk' in img_right.data_vars) and (shift != 0):
                shift_mask = np.zeros(img_right['msk'].data.shape)
                shift_mask[np.where(img_right['msk'].data != img_right.attrs['valid_pixels'])] = np.nan

                # Since the interpolation of the right image is of order 1, the shifted right mask corresponds
                # to an aggregation of two columns of the right mask

                # Create a sliding window of shape 2 using as_strided function : this function create a new a view (by
                # manipulating data pointer)of the shift_mask array with a different shape. The new view pointing to the
                # same memory block as shift_mask so it does not consume any additional memory.
                str_row, str_col = shift_mask.strides
                shape_windows = (shift_mask.shape[0], shift_mask.shape[1] - 1, 2)
                strides_windows = (str_row, str_col, str_col)
                aggregation_window = np.lib.stride_tricks.as_strided(shift_mask, shape_windows, strides_windows,
                                                                     writeable=False)
                shift_mask = np.sum(aggregation_window, 2)
                right_masked += shift_mask

            #  Apply a 3×3 median filter to the input image
            right_masked = filter_.median_filter(right_masked)
            # Convert nan to inf to be able to use the comparison operators < and > in cross_support function
            np.nan_to_num(right_masked, copy=False, nan=np.inf)
            # Compute right cross support using numba to reduce running time
            if offset != 0:
                # Cross support to the size of the cost volume
                cross_right.append(cross_support(right_masked[offset: -offset, offset: -offset], self._cbca_distance,
                                                 self._cbca_intensity))
            else:
                cross_right.append(cross_support(right_masked, self._cbca_distance, self._cbca_intensity))

        return cross_left, cross_right


@njit('f4[:, :](f4[:, :])', cache=True)
def cbca_step_1(cv: np.ndarray) -> np.ndarray:
    """
    Giving the matching cost for one disparity, build a horizontal integral image storing the cumulative row sum,
    S_h(x, y) = S_h(x-1, y) + cv(x, y)

    :param cv: cost volume for the current disparity
    :type cv: 2D np.array (row, col) dtype = np.float32
    :return: the horizontal integral image, step 1
    :rtype: 2D np.array (row, col + 1) dtype = np.float32
    """
    ny_, nx_ = cv.shape
    # Allocate the intermediate cost volume S_h
    # added a column to manage the case in the step 2 : x - left_arm_length -1 = -1
    step1 = np.zeros((ny_, nx_ + 1), dtype=np.float32)

    for y in range(ny_):
        for x in range(nx_):
            # Do not propagate nan
            if not np.isnan(cv[y, x]):
                step1[y, x] = step1[y, x - 1] + cv[y, x]
            else:
                step1[y, x] = step1[y, x - 1]

    return step1


@njit('(f4[:, :], i2[:, :, :], i2[:, :, :], i8[:], i8[:])', cache=True)
def cbca_step_2(step1: np.ndarray, cross_left: np.ndarray, cross_right: np.ndarray, range_col: np.ndarray,
                range_col_right: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """
    Giving the horizontal integral image, computed the horizontal matching cost for one disparity,
    E_h(x, y) = S_h(x + right_arm_length, y) - S_h(x - left_arm_length -1, y)

    :param step1: horizontal integral image, from the cbca_step1, with an extra column that contains 0
    :type step1: 2D np.array (row, col + 1) dtype = np.float32
    :param cross_left: cross support of the left image
    :type cross_left: 3D np.array (row, col, [left, right, top, bot]) dtype=np.int16
    :param cross_right: cross support of the right image
    :type cross_right: 3D np.array (row, col, [left, right, tpo, bot]) dtype=np.int16
    :param range_col: left column for the current disparity (i.e : np.arrange(nb columns), where the correspondent
        in the right image is reachable)
    :type range_col: 1D np.array
    :param range_col_right: right column for the current disparity (i.e : np.arrange(nb columns) - disparity, where
        column - disparity >= 0 and <= nb columns)
    :type range_col_right: 1D np.array
    :return: the horizontal matching cost for the current disparity, and the number of support pixels used for the
        step 2
    :rtype: tuple (2D np.array (row, col) dtype = np.float32, 2D np.array (row, col) dtype = np.float32)
    """
    ny_, nx_ = step1.shape
    # Allocate the intermediate cost volume E_h
    # , remove the extra column from the step 1
    step2 = np.zeros((ny_, nx_ - 1), dtype=np.float32)
    sum_step2 = np.zeros((ny_, nx_ - 1), dtype=np.float32)

    for y in range(step1.shape[0]):
        for x in range(range_col.shape[0]):
            right = min(cross_left[y, range_col[x], 1], cross_right[y, range_col_right[x], 1])
            left = min(cross_left[y, range_col[x], 0], cross_right[y, range_col_right[x], 0])
            step2[y, range_col[x]] = step1[y, range_col[x] + right] - step1[y, range_col[x] - left - 1]
            sum_step2[y, range_col[x]] += (right + left)

    return step2, sum_step2


@njit('f4[:, :](f4[:, :])', cache=True)
def cbca_step_3(step2: np.ndarray) -> np.ndarray:
    """
    Giving the horizontal matching cost, build a vertical integral image for one disparity,
    S_v = S_v(x, y - 1) + E_h(x, y)

    :param step2: horizontal matching cost, from the cbca_step2
    :type step2: 3D xarray.DataArray (row, col, disp)
    :return: the vertical integral image for the current disparity
    :rtype: 2D np.array (row + 1, col) dtype = np.float32
    """
    ny_, nx_ = step2.shape
    # Allocate the intermediate cost volume S_v
    # added a row to manage the case in the step 4 : y - up_arm_length -1 = -1
    step3 = np.zeros((ny_ + 1, nx_), dtype=np.float32)
    step3[0, :] = step2[0, :]

    for y in range(1, ny_):
        for x in range(nx_):
            step3[y, x] = step3[y - 1, x] + step2[y, x]

    return step3


@njit('(f4[:, :], f4[:, :], i2[:, :, :], i2[:, :, :], i8[:], i8[:])', cache=True)
def cbca_step_4(step3: np.ndarray, sum2: np.ndarray, cross_left: np.ndarray, cross_right: np.ndarray,
                range_col: np.ndarray, range_col_right: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """
    Giving the vertical integral image, build the fully aggregated matching cost for one disparity,
    E = S_v(x, y + bottom_arm_length) - S_v(x, y - top_arm_length - 1)

    :param step3: vertical integral image, from the cbca_step3, with an extra row that contains 0
    :type step3: 2D np.array (row + 1, col) dtype = np.float32
    :param sum2: the number of support pixels used for the step 2
    :type sum2: 2D np.array (row, col) dtype = np.float32
    :param cross_left: cross support of the left image
    :type cross_left: 3D np.array (row, col, [left, right, top, bot]) dtype=np.int16
    :param cross_right: cross support of the right image
    :type cross_right: 3D np.array (row, col, [left, right, tpo, bot]) dtype=np.int16
    :param range_col: left column for the current disparity (i.e : np.arrange(nb columns), where the correspondent
        in the right image is reachable)
    :type range_col: 1D np.array
    :param range_col_right: right column for the current disparity (i.e : np.arrange(nb columns) - disparity, where
        column - disparity >= 0 and <= nb columns)
    :type range_col_right: 1D np.array
    :return: the fully aggregated matching cost, and the total number of support pixels used for the aggregation
    :rtype: tuple(2D np.array (row , col) dtype = np.float32, 2D np.array (row , col) dtype = np.float32)
    """
    ny_, nx_ = step3.shape
    # Allocate the final cost volume E
    # , remove the extra row from the step 3
    step4 = np.zeros((ny_ - 1, nx_), dtype=np.float32)
    sum4 = np.copy(sum2)
    for y in range(step4.shape[0]):
        for x in range(range_col.shape[0]):
            top = min(cross_left[y, range_col[x], 2], cross_right[y, range_col_right[x], 2])
            bot = min(cross_left[y, range_col[x], 3], cross_right[y, range_col_right[x], 3])

            step4[y, range_col[x]] = step3[y + bot, range_col[x]] - step3[y - top - 1, range_col[x]]

            sum4[y, range_col[x]] += (top + bot)
            if top != 0:
                sum4[y, range_col[x]] += np.sum(sum2[y - top:y, range_col[x]])
            if bot != 0:
                sum4[y, range_col[x]] += np.sum(sum2[y + 1:y + bot + 1, range_col[x]])

    return step4, sum4


@njit('i2[:, :, :](f4[:, :], i2, f4)', cache=True)
def cross_support(image: np.ndarray, len_arms: int, intensity: float) -> np.ndarray:
    """
    Compute the cross support for an image: find the 4 arms.
    Enforces a minimum support region of 3×3 if pixels are valid.
    The cross support of invalid pixels (pixels that are np.inf) is 0 for the 4 arms.

    :param image: image
    :type image: 2D np.array (row , col) dtype = np.float32
    :param len_arms: maximal length arms
    :param len_arms: int16
    :param intensity: maximal intensity
    :param intensity: float 32
    :return: a 3D np.array ( row, col, [left, right, top, bot] ),
         with the four arms lengths computes for each pixel
    :rtype:  3D np.array ( row, col, [left, right, top, bot] ), dtype=np.int16
    """
    ny_, nx_ = image.shape
    # By default, all cross supports are 0
    cross = np.zeros((ny_, nx_, 4), dtype=np.int16)

    for y in range(ny_):
        for x in range(nx_):

            # If the pixel is valid (np.isfinite = True) compute the cross support
            # Else (np.isfinite = False) the pixel is not valid (no data or invalid) and the cross support value is 0
            # for the 4 arms (default value of the variable cross).
            if np.isfinite(image[y, x]):
                left_len = 0
                left = x
                for left in range(x - 1, max(x - len_arms, -1), -1):
                    if abs(image[y, x] - image[y, left]) >= intensity:
                        break
                    left_len += 1
                # enforces a minimum support region of 3×3 if pixels are valid
                cross[y, x, 0] = max(left_len, 1 * (x >= 1) * np.isfinite(image[y, left]))

                right_len = 0
                right = x
                for right in range(x + 1, min(x + len_arms, nx_)):
                    if abs(image[y, x] - image[y, right]) >= intensity:
                        break
                    right_len += 1
                # enforces a minimum support region of 3×3 if pixels are valid
                cross[y, x, 1] = max(right_len, 1 * (x < nx_ - 1) * np.isfinite(image[y, right]))

                up_len = 0
                up = y
                for up in range(y - 1, max(y - len_arms, -1), -1):
                    if abs(image[y, x] - image[up, x]) >= intensity:
                        break
                    up_len += 1
                # enforces a minimum support region of 3×3 if pixels are valid
                cross[y, x, 2] = max(up_len, 1 * (y >= 1) * np.isfinite(image[up, x]))

                bot_len = 0
                bot = y
                for bot in range(y + 1, min(y + len_arms, ny_)):
                    if abs(image[y, x] - image[bot, x]) >= intensity:
                        break
                    bot_len += 1
                # enforces a minimum support region of 3×3 if pixels are valid
                cross[y, x, 3] = max(bot_len, 1 * (y < ny_ - 1) * np.isfinite(image[bot, x]))

    return cross
