#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2019 Satpy developers
#
# satpy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# satpy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with satpy.  If not, see <http://www.gnu.org/licenses/>.

"""Reader for the FCI L2 products in NetCDF4 format."""

import logging
from contextlib import suppress
from datetime import datetime

import numpy as np
import xarray as xr
from pyresample import geometry

from satpy import CHUNK_SIZE
from satpy.readers._geos_area import get_geos_area_naming, make_ext
from satpy.readers.eum_base import get_service_mode
from satpy.readers.file_handlers import BaseFileHandler
from satpy.resample import get_area_def

logger = logging.getLogger(__name__)

SSP_DEFAULT = 0.0

XY_SCALE_FACTOR_L1C = -5.58871526031607e-5
XY_OFFSET_L1C = 0.155617776423501


class FciL2CommonFunctions(object):
    """Shared operations for file handlers."""

    @property
    def _start_time(self):
        """Get observation start time."""
        return datetime.strptime(self.nc.attrs['time_coverage_start'], '%Y%m%d%H%M%S')

    @property
    def _end_time(self):
        """Get observation end time."""
        return datetime.strptime(self.nc.attrs['time_coverage_end'], '%Y%m%d%H%M%S')

    @property
    def _spacecraft_name(self):
        """Return spacecraft name."""
        return self.nc.attrs['platform']

    @property
    def _sensor_name(self):
        """Return instrument name."""
        return self.nc.attrs['data_source']

    @property
    def ssp_lon(self):
        """Return longitude at subsatellite point."""
        try:
            return float(self.nc['mtg_geos_projection'].attrs['longitude_of_projection_origin'])
        except (KeyError, AttributeError):
            logger.warning(f"ssp_lon could not be obtained from file content, using default value "
                           f"of {SSP_DEFAULT} degrees east instead")
            return SSP_DEFAULT

    def _get_global_attributes(self):
        """Create a dictionary of global attributes to be added to all datasets.

        Returns:
            dict: A dictionary of global attributes.
                filename: name of the product file
                start_time: sensing start time from best available source
                end_time: sensing end time from best available source
                spacecraft_name: name of the spacecraft
                ssp_lon: longitude of subsatellite point
                sensor: name of sensor
                creation_time: creation time of the product
                platform_name: name of the platform

        """
        attributes = {
            'filename': self.filename,
            'start_time': self._start_time,
            'end_time': self._end_time,
            'spacecraft_name': self._spacecraft_name,
            'ssp_lon': self.ssp_lon,
            'sensor': self._sensor_name,
            'creation_time': self.filename_info['creation_time'],
            'platform_name': self._spacecraft_name,
        }
        return attributes

    def __del__(self):
        """Close the NetCDF file that may still be open."""
        with suppress(OSError):
            self.nc.close()


class FciL2NCFileHandler(BaseFileHandler, FciL2CommonFunctions):
    """Reader class for FCI L2 products in NetCDF4 format."""

    def __init__(self, filename, filename_info, filetype_info):
        """Open the NetCDF file with xarray and prepare for dataset reading."""
        super().__init__(filename, filename_info, filetype_info)

        # Use xarray's default netcdf4 engine to open the file
        self.nc = xr.open_dataset(
            self.filename,
            decode_cf=True,
            mask_and_scale=True,
            chunks={
                'number_of_columns': CHUNK_SIZE,
                'number_of_rows': CHUNK_SIZE
            }
        )

        # Read metadata which are common to all datasets
        self.nlines = self.nc['y'].size
        self.ncols = self.nc['x'].size
        self._projection = self.nc['mtg_geos_projection']

    def get_dataset(self, dataset_id, dataset_info):
        """Get dataset using the file_key in dataset_info."""
        var_key = dataset_info['file_key']
        par_name = dataset_info['name']
        logger.debug('Reading in file to get dataset with key %s.', var_key)

        try:
            variable = self.nc[var_key]
        except KeyError:
            logger.warning("Could not find key %s in NetCDF file, no valid Dataset created", var_key)
            return None

        # Compute the area definition
        self._area_def = self._compute_area_def(dataset_id)

        # In some of the test files, invalid pixels may be represented by different values. These are currentlyI
        # identified using "fill_value" and "mask_value" in the YAML file. Any pixel with a value that equals either
        # "fill_value" or "mask_value" will be set to NaN.
        # TODO clean up when test files contain clean data.
        mask_value = dataset_info.get('mask_value', np.NaN)
        fill_value = dataset_info.get('fill_value', np.NaN)

        if dataset_info['file_type'] == 'nc_fci_test_clm':
            data_values = variable.where(variable != fill_value, mask_value).astype('uint32', copy=False)
        else:
            data_values = variable.where(variable != fill_value, mask_value).astype('float32', copy=False)

        data_values.attrs = variable.attrs
        variable = data_values

        # If the variable has 3 dimensions, select the required layer
        if variable.ndim == 3:
            if par_name == 'retrieved_cloud_optical_thickness':
                variable = self.get_total_cot(variable)

            else:
                # Extract data from layer defined in yaml-file
                layer = dataset_info['layer']
                logger.debug('Selecting the layer %d.', layer)
                variable = variable.sel(maximum_number_of_layers=layer)

        if dataset_info['file_type'] == 'nc_fci_test_clm' and var_key != 'cloud_mask_cmrt6_test_result':
            variable.values = (variable.values >> dataset_info['extract_byte'] << 31 >> 31)

        # Rename the dimensions as required by Satpy
        variable = variable.rename({"number_of_rows": 'y', "number_of_columns": 'x'})

        # Manage the attributes of the dataset
        variable.attrs.setdefault('units', None)

        variable.attrs.update(dataset_info)
        variable.attrs.update(self._get_global_attributes())

        return variable

    def get_area_def(self, key):
        """Return the area definition (common to all data in product)."""
        return self._area_def

    def _compute_area_def(self, dataset_id):
        """Compute the area definition.

        Returns:
            AreaDefinition: A pyresample AreaDefinition object containing the area definition.

        """
        area_extent = self._get_area_extent()
        area_naming, proj_dict = self._get_proj_area(dataset_id)
        area_def = geometry.AreaDefinition(
            area_naming['area_id'],
            area_naming['description'],
            "",
            proj_dict,
            self.ncols,
            self.nlines,
            area_extent)

        return area_def

    def _get_area_extent(self):
        """Calculate area extent of dataset."""
        # Load and convert x/y coordinates to degrees as required by the make_ext function
        x = self._get_xy('x')
        y = self._get_xy('y')
        x_deg = np.degrees(x)
        y_deg = np.degrees(y)

        # Select the extreme points and calcualte area extent (not: these refer to pixel center)
        ll_x, ur_x = -x_deg.values[0], -x_deg.values[-1]
        ll_y, ur_y = y_deg.values[-1], y_deg.values[0]
        h = float(self._projection.attrs['perspective_point_height'])
        area_extent_pixel_center = make_ext(ll_x, ur_x, ll_y, ur_y, h)

        # Shift area extent by half a pixel to get the area extent w.r.t. the dataset/pixel corners
        scale_factor = (x[1:]-x[0:-1]).values.mean()
        res = scale_factor * h
        area_extent = tuple(i + res/2 if i > 0 else i - res/2 for i in area_extent_pixel_center)

        return area_extent

    def _get_xy(self, dim):
        """Return xy coordinates of dataset.

        Because of a small error in the scale_factor and add_offset values in the L2 datafile attributes compared to the
        L1C files, an extra check is implemented that recomputes x and y using the correct scale_factor and add_offset.
        """
        coord = self.nc[dim]
        nc_xy = xr.open_dataset(self.filename, mask_and_scale=False)
        scale_factor = nc_xy[dim].attrs['scale_factor']
        offset = nc_xy[dim].attrs['add_offset']

        if (scale_factor != np.float32(XY_SCALE_FACTOR_L1C)) or (offset != np.float32(XY_OFFSET_L1C)):
            coord = (coord - offset) / scale_factor
            coord = coord * np.float32(XY_SCALE_FACTOR_L1C) + np.float32(XY_OFFSET_L1C)

        return coord

    def _get_proj_area(self, dataset_id):
        """Extract projection and area information."""
        # Read the projection data from the mtg_geos_projection variable
        a = float(self._projection.attrs['semi_major_axis'])
        rf = float(self._projection.attrs['inverse_flattering'])
        h = float(self._projection.attrs['perspective_point_height'])

        res = dataset_id.resolution

        area_naming_input_dict = {'platform_name': 'mtg',
                                  'instrument_name': 'fci',
                                  'resolution': res,
                                  }

        area_naming = get_geos_area_naming({**area_naming_input_dict,
                                            **get_service_mode('fci', self.ssp_lon)})

        proj_dict = {'a': a,
                     'lon_0': self.ssp_lon,
                     'h': h,
                     "rf": rf,
                     'proj': 'geos',
                     'units': 'm',
                     "sweep": 'y'}

        return area_naming, proj_dict

    @staticmethod
    def get_total_cot(variable):
        """Sum the cloud optical thickness from the two OCA layers.

        The optical thickness has to be transformed to linear space before adding the values from the two layers. The
        combined/total optical thickness is then transformed back to logarithmic space.
        """
        attrs = variable.attrs
        variable = 10 ** variable
        variable = variable.fillna(0.)
        variable = variable.sum(dim='maximum_number_of_layers', keep_attrs=True)
        variable = variable.where(variable != 0., np.nan)
        variable = np.log10(variable)
        variable.attrs = attrs

        return variable


class FciL2NCSegmentFileHandler(BaseFileHandler, FciL2CommonFunctions):
    """Reader class for FCI L2 Segmented products in NetCDF4 format."""

    def __init__(self, filename, filename_info, filetype_info):
        """Open the NetCDF file with xarray and prepare for dataset reading."""
        super().__init__(filename, filename_info, filetype_info)
        # Use xarray's default netcdf4 engine to open the file
        self.nc = xr.open_dataset(
            self.filename,
            decode_cf=True,
            mask_and_scale=True,
            chunks={
                'number_of_FoR_cols': CHUNK_SIZE,
                'number_of_FoR_rows': CHUNK_SIZE
            }
        )

        # Read metadata which are common to all datasets
        self.nlines = self.nc['number_of_FoR_rows'].size
        self.ncols = self.nc['number_of_FoR_cols'].size

    def get_area_def(self, key):
        """Return the area definition (common to all data in product)."""
        return self._area_def

    def _construct_area_def(self, dataset_id):
        """Construct the area definition.

        Returns:
            AreaDefinition: A pyresample AreaDefinition object containing the area definition.

        """
        # TODO make sure that the lat/lons from the AreaDefinition match the latitude and longitude arrays stored in
        #  the segmented product files.
        res = dataset_id.resolution

        area_naming_input_dict = {'platform_name': 'mtg',
                                  'instrument_name': 'fci',
                                  'resolution': res,
                                  }

        area_naming = get_geos_area_naming({**area_naming_input_dict,
                                            **get_service_mode('fci', self.ssp_lon)})

        # Construct area definition from standardized area definition.
        stand_area_def = get_area_def(area_naming['area_id'])

        if (stand_area_def.x_size != self.ncols) | (stand_area_def.y_size != self.nlines):
            raise NotImplementedError('Unrecognised area definition.')

        mod_area_extent = self._modify_area_extent(stand_area_def.area_extent)

        area_def = geometry.AreaDefinition(
            stand_area_def.area_id,
            stand_area_def.description,
            "",
            stand_area_def.proj_dict,
            stand_area_def.x_size,
            stand_area_def.y_size,
            mod_area_extent)

        return area_def

    @staticmethod
    def _modify_area_extent(stand_area_extent):
        """Modify area extent to macth satellite projection.

        Area extent has to be modified since the L2 products are stored with the south-east
        in the upper-right corner (as opposed to north-east in the standardized area definitions).
        """
        ll_x, ll_y, ur_x, ur_y = stand_area_extent
        ll_y *= -1.
        ur_y *= -1.
        area_extent = tuple([ll_x, ll_y, ur_x, ur_y])

        return area_extent

    def get_dataset(self, dataset_id, dataset_info):
        """Get dataset using the file_key in dataset_info."""
        var_key = dataset_info['file_key']
        logger.debug('Reading in file to get dataset with key %s.', var_key)

        try:
            variable = self.nc[var_key]
        except KeyError:
            logger.warning("Could not find key %s in NetCDF file, no valid Dataset created", var_key)
            return None

        # Compute the area definition
        self._area_def = self._construct_area_def(dataset_id)

        # In some of the test files, invalid pixels may be represented by different values. These are currentlyI
        # identified using "fill_value" and "mask_value" in the YAML file. Any pixel with a value that equals either
        # "fill_value" or "mask_value" will be set to NaN.
        # TODO clean up when test files contain clean data.
        mask_value = dataset_info.get('mask_value', np.NaN)
        fill_value = dataset_info.get('fill_value', np.NaN)

        float_variable = variable.where(variable != fill_value, mask_value).astype('float32', copy=False)
        float_variable.attrs = variable.attrs
        variable = float_variable

        if any(dim in dataset_info.keys() for dim in ['category_id', 'channel_id', 'vis_channel_id', 'ir_channel_id']):
            variable = self._slice_dataset(variable, dataset_info)

        # Rename the dimensions as required by Satpy
        variable = variable.rename({"number_of_FoR_rows": 'y', "number_of_FoR_cols": 'x'})
        variable.attrs.setdefault('units', None)

        variable.attrs.update(dataset_info)
        variable.attrs.update(self._get_global_attributes())

        return variable

    @staticmethod
    def _slice_dataset(variable, dataset_info):
        """Slice data if dimension layers have been provided in yaml-file."""
        if 'number_of_categories' in variable.dims:
            cat_id = dataset_info.get('category_id', None)
            if cat_id is not None:
                logger.debug('Selecting category-id %i.' % cat_id)
                variable = variable.sel(number_of_categories=cat_id)

        if 'number_of_channels' in variable.dims:
            channel_id = dataset_info.get('channel_id', None)
            if channel_id is not None:
                logger.debug('Selecting FCI channel-id %i.' % channel_id)
                variable = variable.sel(number_of_channels=channel_id)

        if 'number_of_vis_channels' in variable.dims:
            channel_id = dataset_info.get('vis_channel_id', None)
            if channel_id is not None:
                logger.debug('Selecting FCI VIS/NIR channel-id %i.' % channel_id)
                variable = variable.sel(number_of_vis_channels=channel_id, )

        if 'number_of_ir_channels' in variable.dims:
            channel_id = dataset_info.get('ir_channel_id', None)
            if channel_id is not None:
                logger.debug('Selecting FCI IR channel-id %i.' % channel_id)
                variable = variable.sel(number_of_ir_channels=channel_id)

        return variable
