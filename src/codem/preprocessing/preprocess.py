"""
preprocess.py
Project: CRREL-NEGGS University of Houston Collaboration
Date: February 2021

This module contains classes and methods for preparing Point Cloud, Mesh, and
DSM geospatial data types for co-registration. The primary tasks are:

* Estimating data density - this is information is used to set the resolution of
  the data used in the registration modules
* Converting all data types to a DSM - The registration modules operate on a
  gridded version of data being registered. Disorganized data is gridded into a
  DSM, voids are filled, and long wavelength elevation relief removed to allow
  storage of local elevation changes in 8-bit grayscale.
* Point cloud and normal vector generation - the fine registration module
  requires an array of 3D points and normal vectors for a point-to-plane ICP
  solution. These data are derived from the gridded DSM.

This module contains the following classes and methods:

* GeoData - parent class for geospatial data, NOT to be instantiated directly
* DSM - class for Digital Surface Model data
* PointCloud - class for Point Cloud data
* Mesh - class for Mesh data
* instantiate - method for auto-instantiating the appropriate class
"""
import json
import logging
import math
import os
import tempfile
from typing import Any
from typing import Dict
from typing import Optional
from typing import Tuple

import codem.lib.resources as r
import cv2
import numpy as np
import pdal
import pyproj
import rasterio.fill
import rasterio.transform
import trimesh
from rasterio import windows
from rasterio.coords import BoundingBox
from rasterio.coords import disjoint_bounds
from rasterio.crs import CRS
from rasterio.enums import Resampling
from typing_extensions import TypedDict


class RegistrationParameters(TypedDict):
    matrix: np.ndarray
    omega: np.float64
    phi: np.float64
    kappa: np.float64
    trans_x: np.float64
    trans_y: np.float64
    trans_z: np.float64
    scale: np.float64
    n_pairs: np.int64
    rmse_x: np.float64
    rmse_y: np.float64
    rmse_z: np.float64
    rmse_3d: np.float64


logger = logging.getLogger(__name__)


class GeoData:
    """
    A class for storing and preparing geospatial data

    Parameters
    ----------
    config: dict
        Dictionary of configuration options
    fnd: bool
        Whether the file is foundation data

    Methods
    -------
    _read_dsm
    _get_nodata_mask
    _infill
    _normalize
    _dsm2pc
    _generate_vectors
    prep
    """

    def __init__(self, config: dict, fnd: bool) -> None:
        self.logger = logging.getLogger(__name__)
        self.file = config["FND_FILE"] if fnd else config["AOI_FILE"]
        self.fnd = fnd
        self._type = "undefined"
        self.nodata = None
        self.dsm = np.empty((0, 0), dtype=np.double)
        self.point_cloud = np.empty((0, 0), dtype=np.double)
        self.crs = None
        self.transform: Optional[rasterio.Affine] = None
        self.area_or_point = "Undefined"
        self.normed = np.empty((0, 0), dtype=np.uint8)
        self.normal_vectors = np.empty((0, 0), dtype=np.double)
        self.processed = False
        self._resolution = 0.0
        self.native_resolution = 0.0
        self.units_factor = 1.0
        self.units: Optional[str] = None
        self.weak_size = config["DSM_WEAK_FILTER"]
        self.strong_size = config["DSM_STRONG_FILTER"]
        self.config = config
        self.bound_slices: Optional[Tuple[slice, slice]] = None
        self.window: Optional[windows.Window] = None

    @property
    def type(self) -> str:
        return self._type

    @type.setter
    def type(self, value: str) -> None:
        self._type = value

    @property
    def resolution(self) -> float:
        return self._resolution

    @resolution.setter
    def resolution(self, value: float) -> None:
        if value <= 0.0:
            raise ValueError("Resolution must be greater than 0")
        self._resolution = value
        return None

    def _read_dsm(self, file_path: str, force: bool = False) -> None:
        """
        Reads in DSM data from a given file path.

        Parameters
        ----------
        file_path: str
            Path to DSM data
        """
        tag = ["AOI", "Foundation"][int(self.fnd)]

        if self.dsm.size == 0 or force:
            with rasterio.open(file_path) as data:
                self.dsm = data.read(1, window=self.window)
                if self.window is None:
                    self.transform = data.transform
                else:
                    self.transform = data.window_transform(self.window)
                self.nodata = data.nodata
                self.crs = data.crs
                tags = data.tags()
            if "AREA_OR_POINT" in tags and tags["AREA_OR_POINT"] == "Area":
                self.area_or_point = "Area"
            elif "AREA_OR_POINT" in tags and tags["AREA_OR_POINT"] == "Point":
                self.area_or_point = "Point"
            else:
                self.area_or_point = "Area"
                self.logger.debug(
                    f"'AREA_OR_POINT' not supplied in {tag}-{self.type.upper()} - defaulting to 'Area'"
                )

        if self.nodata is None:
            self.logger.info(f"{tag}-{self.type.upper()} does not have a nodata value.")
        if self.transform == rasterio.Affine.identity():
            self.logger.warning(f"{tag}-{self.type.upper()} has an identity transform.")

    def _get_nodata_mask(self, dsm: np.ndarray) -> np.ndarray:
        """
        Generates a binary array indicating invalid data locations in the
        passed array. Invalid data are NaN and nodata values. A value of '1'
        indicates valid data locations. '0' indicates invalid data locations.

        Parameters
        ----------
        dsm: np.array
            Array containing digital surface model elevation data

        Returns
        -------
        mask: np.array
            The binary mask
        """
        nan_mask = np.isnan(dsm)
        mask: np.ndarray
        if self.nodata is not None:
            dsm[nan_mask] = self.nodata
            mask = dsm != self.nodata
        else:
            mask = ~nan_mask

        return mask.astype(np.uint8)

    def _infill(self) -> None:
        """
        Infills pixels flagged as invalid (via the nodata value or NaN values)
        via rasterio's inverse distance weighting interpolation. Necessary to
        mitigate spurious feature detection.
        """
        dsm_array = np.array(self.dsm)
        if self.nodata is not None:
            empty_array = np.full(dsm_array.shape, self.nodata)
        else:
            empty_array = np.empty_like(dsm_array)

        assert not np.array_equal(dsm_array, empty_array), "DSM array is empty."

        infilled = np.copy(self.dsm)
        mask = self._get_nodata_mask(infilled)
        infill_mask = np.copy(mask)

        while np.sum(infill_mask) < infill_mask.size:
            infilled = rasterio.fill.fillnodata(infilled, mask=infill_mask)
            infill_mask = self._get_nodata_mask(infilled)
        self.infilled = infilled
        self.nodata_mask = mask

    def _normalize(self) -> None:
        """
        Suppresses high frequency information and removes long wavelength
        topography with a bandpass filter. Normalizes the result to fit in an
        8-bit range. We scale the strong and weak filter sizes to convert them
        from object space distance to pixels.
        """
        if self.transform is None:
            raise RuntimeError(
                "self.transform is not initialized, you run the prep() method?"
            )
        # this step gets tripped up by GCS since the transform numbers are going to be tiny!
        breakpoint()
        scale = np.sqrt(self.transform[0] ** 2 + self.transform[1] ** 2)
        weak_filtered = cv2.GaussianBlur(self.infilled, (0, 0), self.weak_size / scale)
        strong_filtered = cv2.GaussianBlur(
            self.infilled, (0, 0), self.strong_size / scale
        )
        bandpassed = weak_filtered - strong_filtered
        low = np.percentile(bandpassed, 1)
        high = np.percentile(bandpassed, 99)
        clipped = np.clip(bandpassed, low, high)
        normalized = (clipped - low) / (high - low)
        quantized = (255 * normalized).astype(np.uint8)
        self.normed = quantized

    def _dsm2pc(self) -> None:
        """
        Converts DSM data to point cloud data. If the DSM was saved with the
        AREA_OR_POINT tag set to 'Area', then we adjust the pixel values by 0.5
        pixel. This is because we assume the DSM elevation value to represent
        the elevation at the center of the pixel, not the upper left corner.
        """
        if self.transform is None:
            raise RuntimeError(
                "self.transform needs to be set to a rasterio.Affine object"
            )
        rows = np.arange(self.dsm.shape[0], dtype=np.float64)
        cols = np.arange(self.dsm.shape[1], dtype=np.float64)
        uu, vv = np.meshgrid(cols, rows)
        u = np.reshape(uu, -1)
        v = np.reshape(vv, -1)

        if self.area_or_point == "Area":
            u += 0.5
            v += 0.5

        xy = np.asarray(self.transform * (u, v))
        z = np.reshape(self.dsm, -1)
        xyz = np.vstack((xy, z)).T

        mask = np.reshape(np.array(self.nodata_mask, dtype=bool), -1)
        xyz = xyz[mask]

        self.point_cloud = xyz

    def _generate_vectors(self) -> None:
        """
        Generates normal vectors, required for the ICP registration module, from
        the point cloud data. PDAL is used for speed.
        """
        k = 9
        n_points = self.point_cloud.shape[0]

        if n_points < k:
            raise RuntimeError(
                f"Point cloud must have at least {k} points to generate normal vectors"
            )
        xyz_dtype = np.dtype([("X", np.double), ("Y", np.double), ("Z", np.double)])
        xyz = np.empty(self.point_cloud.shape[0], dtype=xyz_dtype)
        xyz["X"] = self.point_cloud[:, 0]
        xyz["Y"] = self.point_cloud[:, 1]
        xyz["Z"] = self.point_cloud[:, 2]
        pipe = [
            {"type": "filters.normal", "knn": k},
        ]
        p = pdal.Pipeline(
            json.dumps(pipe),
            arrays=[
                xyz,
            ],
        )
        p.execute()

        arrays = p.arrays
        array = arrays[0]
        filtered_normals = np.vstack(
            (array["NormalX"], array["NormalY"], array["NormalZ"])
        ).T
        self.normal_vectors = filtered_normals

    def _calculate_resolution(self) -> None:
        raise NotImplementedError

    def _create_dsm(self, resample: bool = True) -> None:
        raise NotImplementedError

    def prep(self) -> None:
        """
        Prepares data for registration.
        """
        tag = ["AOI", "Foundation"][int(self.fnd)]
        self.logger.info(f"Preparing {tag}-{self.type.upper()} for registration.")
        self._infill()
        self.logger.info(f"Preparing {tag}-{self.type.upper()} for normalization.")
        self._normalize()
        self.logger.info(f"Preparing {tag}-{self.type.upper()} for point cloudification.")
        self._dsm2pc()

        if self.fnd:
            self._generate_vectors()

        self.processed = True

    def _debug_plot(self, keypoints: Optional[np.ndarray] = None) -> None:
        """Use this to show the raster"""
        import matplotlib.pyplot as plt

        if hasattr(self, "infilled"):
            plt.imshow(self.infilled, cmap="gray")
        else:
            plt.imshow(self.dsm, cmap="gray")
        if keypoints is not None:
            plt.scatter(
                keypoints[:, 0], keypoints[:, 1], marker="s", color="orange", s=10.0
            )
        plt.show()


class DSM(GeoData):
    """
    A class for storing and preparing Digital Surface Model (DSM) data.
    """

    def __init__(self, config: dict, fnd: bool) -> None:
        super().__init__(config, fnd)
        self.type = "dsm"
        self._calculate_resolution()

    def _create_dsm(self, resample: bool = True) -> None:
        """
        Resamples the DSM to the registration pipeline resolution and applies
        a scale factor to convert to meters.
        """

        #might just have to project if in GCS.... but maybe there is a non-intrusive way to have the units and necessary scaling things converted to meters where they need to be.

        with rasterio.open(self.file) as data:
            resample_factor = (
                self.native_resolution / self.resolution if resample else 1.0
            )
            tag = ["AOI", "Foundation"][int(self.fnd)]
            breakpoint()
            if resample_factor != 1 or self.native_resolution != self.resolution:
                self.logger.info(
                    f"Resampling {tag}-{self.type.upper()} to a pixel resolution of: {self.resolution} meters"
                )
                # data is read as float32 as int dtypes result in poor keypoint identification
                self.dsm = data.read(
                    1,
                    out_shape=(
                        data.count,
                        int(data.height * resample_factor),
                        int(data.width * resample_factor),
                    ),
                    resampling=Resampling.cubic,
                    out_dtype=np.float32,
                    window=self.window,
                )
                # We post-multiply the transform by the resampling scale. This does
                # not change the origin coordinates, only the pixel scale.
                if self.window is None:
                    self.transform = data.transform * data.transform.scale(
                        (data.width / self.dsm.shape[-1]),
                        (data.height / self.dsm.shape[-2]),
                    )
                else:
                    transform = data.window_transform(self.window)
                    self.transform = transform * transform.scale(
                        (data.width / self.dsm.shape[1]),
                        (data.height / self.dsm.shape[0]),
                    )
            else:
                self.logger.info(
                    f"No resampling required for {tag}-{self.type.upper()}"
                )
                # data is read as float32 as int dtypes result in poor keypoint identification
                self.dsm = data.read(1, out_dtype=np.float32, window=self.window)
                if self.window is None:
                    self.transform = data.transform
                else:
                    self.transform = data.window_transform(self.window)
            self.nodata = data.nodata
            self.crs = data.crs

            # Scale the elevation values into meters
            mask = (self._get_nodata_mask(self.dsm)).astype(bool)
            if np.can_cast(self.units_factor, self.dsm.dtype, casting="same_kind"):
                self.dsm[mask] *= self.units_factor
            elif isinstance(self.units_factor, float):
                if self.units_factor.is_integer():
                    self.dsm[mask] *= int(self.units_factor)
                else:
                    self.logger.warning(
                        "Cannot safely scale DSM by units factor, attempting to anyway!"
                    )
                    self.dsm[mask] = np.multiply(
                        self.dsm, self.units_factor, where=mask, casting="unsafe"
                    )
            else:
                raise TypeError(
                    f"Type of {self.units_factor} needs to be a float, is "
                    f"{type(self.units_factor)}"
                )

            # We pre-multiply the transform by the unit change scale. This scales
            # the origin coordinates into meters and also changes the pixel scale
            # into meters.
            self.transform = (
                data.transform.scale(self.units_factor, self.units_factor)
                * self.transform
            )

            tags = data.tags()
            if "AREA_OR_POINT" in tags and tags["AREA_OR_POINT"] == "Area":
                self.area_or_point = "Area"
            elif "AREA_OR_POINT" in tags and tags["AREA_OR_POINT"] == "Point":
                self.area_or_point = "Point"
            else:
                self.area_or_point = "Area"
                self.logger.debug(
                    f"'AREA_OR_POINT' not supplied in {tag}-{self.type.upper()} - defaulting to 'Area'"
                )
        if self.nodata is None:
            self.logger.info(f"{tag}-{self.type.upper()} does not have a nodata value.")
        if self.transform == rasterio.Affine.identity():
            self.logger.warning(f"{tag}-{self.type.upper()} has an identity transform.")

    def _calculate_resolution(self) -> None:
        """
        Calculates the pixel resolution of the DSM file.
        """
        with rasterio.open(self.file) as data:
            T = data.transform
            if T.is_identity:
                raise ValueError(
                    f"{os.path.basename(self.file)} has no transform data associated "
                    "with it."
                )
            if not T.is_conformal:
                raise ValueError(
                    f"{os.path.basename(self.file)} cannot contain a rotation angle."
                )

            scales = T._scaling
            if scales[0] != scales[1]:
                raise ValueError(
                    f"{os.path.basename(self.file)} has different X and Y scales, "
                    "they must be identical"
                )

            tag = ["AOI", "Foundation"][int(self.fnd)]

            if data.crs is None:
                self.logger.warning(
                    f"Linear unit for {tag}-{self.type.upper()} not detected -> "
                    "meters assumed"
                )
                self.native_resolution = abs(T.a)
            
            elif not data.crs.is_projected:
                self.logger.warning(
                    f"Coordinate system for {tag}-{self.type.upper()} not projected -> "
                    f"meters assumed. For more accurate results, put {tag}-{self.type.upper()} "
                    "in projected coordinate system."
                )




                # need to calculate the degree resolution to the meter resolution (not super accurately), maybe warn the user that it will not be as accurate as it can be.
                unit_factor = data.crs.units_factor[1]
                radians = T[0]/unit_factor
                # haversine formula
                lon1 = 0
                lat1 = 0
                lon2 = radians
                lat2 = 0
                dlon = lon2 - lon1
                dlat = lat2 - lat1
                a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
                c = 2 * math.asin(math.sqrt(a))
                # self.native_resolution = c
                # src_transform = self.transform
                # dst_transform = rasterio.Affine(c, src_transform[1], src_transform[2], c, src_transform[4], src_transform[5])
                # self.transform = dst_transform
                # breakpoint()

                # project to web mercator

                dst_crs = 'EPSG:3857'

                with rasterio.open(os.path.normpath(self.file)) as src:
                    transform, width, height = rasterio._warp._calculate_default_transform(
                    src.crs, dst_crs, src.width, src.height, *src.bounds)
                    kwargs = src.meta.copy()
                    kwargs.update({
                        'crs': dst_crs,
                        'transform': transform,
                        'width': width,
                        'height': height
                    })


                    with rasterio.open('intermediate.tif', 'w', **kwargs) as dst:
                        for i in range(1, src.count + 1):
                            rasterio._warp._reproject(
                                source=rasterio.band(src, i),
                                destination=rasterio.band(dst, i),
                                src_transform=src.transform,
                                src_crs=src.crs,
                                dst_transform=transform,
                                dst_crs=dst_crs,
                                resampling=Resampling.nearest)
                self.transform = transform
                self.native_resolution = abs(self.transform[0])

                breakpoint()

            else:
                self.logger.info(
                    f"Linear unit for {tag}-{self.type.upper()} detected as "
                    f"{data.crs.linear_units}"
                )
                self.units_factor = data.crs.linear_units_factor[1]
                self.units = data.crs.linear_units
                self.native_resolution = abs(T.a) * self.units_factor
        self.logger.info(
            f"Calculated native resolution of {tag}-{self.type.upper()} as: "
            f"{self.native_resolution:.1f} meters"
        )


class PointCloud(GeoData):
    """
    A class for storing and preparing Point Cloud data.
    """

    def __init__(self, config: dict, fnd: bool) -> None:
        super().__init__(config, fnd)
        self.type = "pcloud"
        self._calculate_resolution()

    def _create_dsm(self, resample: bool = True) -> None:
        """
        Converts the point cloud to meters and rasters it to a DSM.
        """
        tag = ["AOI", "Foundation"][int(self.fnd)]
        self.logger.info(
            f"Extracting DSM from {tag}-{self.type.upper()} with resolution of: {self.resolution} meters"
        )

        # Scale matrix formatted for PDAL consumption
        units_transform = (
            f"{self.units_factor} 0 0 0 "
            f"0 {self.units_factor} 0 0 "
            f"0 0 {self.units_factor} 0 "
            "0 0 0 1"
        )

        file_handle, tmp_file = tempfile.mkstemp(suffix=".tif")

        pipe = [
            self.file,
            {"type": "filters.transformation", "matrix": units_transform},
            {
                "type": "writers.gdal",
                "resolution": self.resolution,
                "output_type": "max",
                "nodata": -9999.0,
                "filename": tmp_file,
            },
        ]

        p = pdal.Pipeline(json.dumps(pipe))
        p.execute()

        self._read_dsm(tmp_file, force=True)
        os.close(file_handle)
        os.remove(tmp_file)

    def _calculate_resolution(self) -> None:
        """
        Calculates point cloud average point spacing.
        """
        pdal_pipeline = [
            self.file,
            {"type": "filters.hexbin", "edge_size": 25, "threshold": 1},
        ]
        pipeline = pdal.Pipeline(json.dumps(pdal_pipeline))
        pipeline.execute()

        tag = ["AOI", "Foundation"][int(self.fnd)]
        metadata = pipeline.metadata["metadata"]
        reader_metadata = [val for key, val in metadata.items() if "readers" in key]
        # breakpoint()
        crs = CRS.from_string(reader_metadata[0]["srs"]["horizontal"])
        if reader_metadata[0]["srs"]["horizontal"] == "":
            self.logger.warning(
                f"Linear unit for {tag}-{self.type.upper()} not detected --> meters assumed"
            )
            spacing = metadata["filters.hexbin"]["avg_pt_spacing"]
        
        elif not crs.is_projected:
            self.logger.warning(
                f"Coordinate system for {tag}-{self.type.upper()} not projected --> meters assumed"
            )
            spacing = metadata["filters.hexbin"]["avg_pt_spacing"]
        
        else:
            crs = CRS.from_string(reader_metadata[0]["srs"]["horizontal"])
            self.logger.info(
                f"Linear unit for {tag}-{self.type.upper()} detected as {crs.linear_units}."
            )
            spacing = (
                crs.linear_units_factor[1]
                * metadata["filters.hexbin"]["avg_pt_spacing"]
            )
            self.units_factor = crs.linear_units_factor[1]
            self.units = crs.linear_units

        self.logger.info(
            f"Calculated native resolution for {tag}-{self.type.upper()} as: {spacing:.1f} meters"
        )

        self.native_resolution = spacing


class Mesh(GeoData):
    """
    A class for storing and preparing Mesh data.
    """

    def __init__(self, config: dict, fnd: bool) -> None:
        super().__init__(config, fnd)
        self.type = "mesh"
        self._calculate_resolution()

    def _create_dsm(self, resample: bool = True) -> None:
        """
        Converts mesh vertices to meters and rasters them to a DSM.
        """
        tag = ["AOI", "Foundation"][int(self.fnd)]
        self.logger.info(
            f"Extracting DSM from {tag}-{self.type.upper()} with resolution of: {self.resolution} meters"
        )

        mesh = trimesh.load_mesh(self.file)
        vertices = mesh.vertices

        xyz_dtype = np.dtype([("X", np.double), ("Y", np.double), ("Z", np.double)])
        xyz = np.empty(vertices.shape[0], dtype=xyz_dtype)
        xyz["X"] = vertices[:, 0]
        xyz["Y"] = vertices[:, 1]
        xyz["Z"] = vertices[:, 2]

        # Scale matrix formatted for PDAL consumption
        units_transform = (
            f"{self.units_factor} 0 0 0 "
            f"0 {self.units_factor} 0 0 "
            f"0 0 {self.units_factor} 0 "
            "0 0 0 1"
        )

        pipe = [
            self.file,
            {
                "type": "filters.transformation",
                "matrix": units_transform,
            },
            {
                "type": "writers.gdal",
                "resolution": self.resolution,
                "output_type": "max",
                "nodata": -9999.0,
                "filename": "temp_dsm.tif",
            },
        ]
        p = pdal.Pipeline(
            json.dumps(pipe),
            arrays=[
                xyz,
            ],
        )
        p.execute()

        self._read_dsm("temp_dsm.tif")
        os.remove("temp_dsm.tif")

    def _calculate_resolution(self) -> None:
        """
        Calculates mesh average vertex spacing.
        """
        pdal_pipeline = [
            self.file,
            {"type": "filters.hexbin", "edge_size": 25, "threshold": 1},
        ]
        pipeline = pdal.Pipeline(json.dumps(pdal_pipeline))
        pipeline.execute()
        # metadata = json.loads(pipeline.metadata)["metadata"]
        metadata = pipeline.metadata["metadata"]
        spacing = metadata["filters.hexbin"]["avg_pt_spacing"]

        mesh = trimesh.load_mesh(self.file)
        tag = ["AOI", "Foundation"][int(self.fnd)]

        if not hasattr(mesh, "units") or mesh.units is None:
            self.logger.warning(
                f"Linear unit for {tag}-{self.type.upper()} not detected --> meters assumed"
            )
            self.units_factor = 1.0
            self.units = "meters"
        else:
            self.logger.info(
                f"Linear unit for {tag}-{self.type.upper()} detected as {mesh.units}"
            )
            self.units_factor = trimesh.units.unit_conversion(mesh.units, "meters")
            self.units = mesh.units
            spacing *= self.units_factor

        self.logger.info(
            f"Calculated native resolution for {tag}-{self.type.upper()} as: {spacing:.1f} meters"
        )

        self.native_resolution = spacing


def instantiate(config: dict, fnd: bool) -> GeoData:
    """
    Factory method for auto-instantiating the appropriate data class.

    Parameters
    ----------
    file_path: str
        Path to data file
    fnd: bool
        Whether the file is the foundation object

    Returns
    -------
    Type[G]
        An instance of the appropriate child class of GeoData
    """
    file_path = config["FND_FILE"] if fnd else config["AOI_FILE"]
    if os.path.splitext(file_path)[-1] in r.dsm_filetypes:
        return DSM(config, fnd)
    if os.path.splitext(file_path)[-1] in r.mesh_filetypes:
        return Mesh(config, fnd)
    if os.path.splitext(file_path)[-1] in r.pcloud_filetypes:
        return PointCloud(config, fnd)
    logger.warning(f"File {file_path} has an unsupported type.")
    raise NotImplementedError("File type not currently supported.")


def clip_data(fnd_obj: GeoData, aoi_obj: GeoData, config: Dict[str, Any]) -> None:
    # how much outside of the bounds to search for registration features
    oversize_scale = 1.5

    if not config["TIGHT_SEARCH"]:
        return None

    # is foundation CRS defined:
    if any(crs is None for crs in (fnd_obj.crs, aoi_obj.crs)):
        raise AttributeError(
            "To perform this operation, the CRS of both datasets must be defined and equal"
        )

    foundation_crs = pyproj.CRS(fnd_obj.crs)
    compliment_crs = pyproj.CRS(aoi_obj.crs)

    if not foundation_crs.equals(compliment_crs):
        raise ValueError(
            "To perform this operation, the CRS of both datasets must be equal"
        )

    # create our original and scaled bounding boxes (only handles right/bottom)
    original_bounding_boxes: Dict[str, BoundingBox] = {}
    scaled_bounding_boxes: Dict[str, BoundingBox] = {}
    for dataset in [fnd_obj, aoi_obj]:
        for scaling in (1.0, oversize_scale):
            if math.isclose(scaling, 1.0):
                bounding_boxes = original_bounding_boxes
            else:
                bounding_boxes = scaled_bounding_boxes
            key = "foundation" if dataset.fnd else "compliment"
            if dataset.transform is None:
                raise RuntimeError("Transform needs to be specified for the datasets")

            transform = dataset.transform * dataset.transform.scale(scaling)
            left, top = transform * (0, 0)
            right, bottom = transform * dataset.dsm.shape
            bounding_boxes[key] = BoundingBox(left, bottom, right, top)

    # need to adjust scale on left and top due to transform scaling math
    for dataset in [fnd_obj, aoi_obj]:
        key = "foundation" if dataset.fnd else "compliment"
        x_expanded = abs(
            scaled_bounding_boxes[key].right - original_bounding_boxes[key].right
        )
        y_expanded = abs(
            scaled_bounding_boxes[key].bottom - original_bounding_boxes[key].bottom
        )
        left_new = scaled_bounding_boxes[key].left - x_expanded
        top_new = scaled_bounding_boxes[key].top + y_expanded

        right_new = scaled_bounding_boxes[key].right
        bottom_new = scaled_bounding_boxes[key].bottom
        scaled_bounding_boxes[key] = BoundingBox(
            left_new, bottom_new, right_new, top_new
        )

    if disjoint_bounds(bounding_boxes["foundation"], bounding_boxes["compliment"]):
        raise ValueError("Bounding boxes for foundation and compliment are disjoint")

    # get new bounding boxes
    clipped_bounding_boxes = compute_clipped_bounds(
        original_bounding_boxes, scaled_bounding_boxes
    )

    for key in ("foundation", "compliment"):
        dataset_obj = fnd_obj if key == "foundation" else aoi_obj
        transform = rasterio.transform.AffineTransformer(dataset_obj.transform)
        xs, ys = transform.rowcol(
            [clipped_bounding_boxes[key].left, clipped_bounding_boxes[key].right],
            [clipped_bounding_boxes[key].top, clipped_bounding_boxes[key].bottom],
        )
        dataset_obj.window = windows.Window.from_slices(slice(*xs), slice(*ys))
        # need that we know the window, create the DSM with resampling
        dataset_obj._create_dsm(resample=True)
    return None


def compute_clipped_bounds(
    original: Dict[str, BoundingBox], scaled: Dict[str, BoundingBox]
) -> Dict[str, BoundingBox]:
    foundation_original = original["foundation"]
    foundation_scaled = scaled["foundation"]
    compliment_original = original["compliment"]
    compliment_scaled = scaled["compliment"]

    trimmed_foundation: Dict[str, float] = {}
    trimmed_compliment: Dict[str, float] = {}
    sides = foundation_original._fields
    for fixed in ("foundation", "compliment"):
        if fixed == "foundation":
            new_bounds = trimmed_foundation
            scaled = compliment_scaled
            edge = foundation_original
        else:
            new_bounds = trimmed_compliment
            scaled = foundation_scaled
            edge = compliment_original

        for side in sides:
            closer = max if side in ("left", "bottom") else min
            new_bounds[side] = closer(getattr(edge, side), getattr(scaled, side))
    clipped_bounds: Dict[str, BoundingBox] = {
        "foundation": BoundingBox(
            *[trimmed_foundation[side] for side in ("left", "bottom", "right", "top")]
        )
    }
    clipped_bounds["compliment"] = BoundingBox(
        *[trimmed_compliment[side] for side in ("left", "bottom", "right", "top")]
    )
    return clipped_bounds
