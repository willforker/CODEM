import os
import json
import subprocess

conda_json = json.loads(
    subprocess.run(
        ["conda", "info", "--json"], capture_output=True
    ).stdout
)
CONDA_PREFIX = conda_json["env_vars"]["CONDA_PREFIX"]

# set environment variables
os.environ["PATH"] = f"{os.environ['PATH']};{os.path.join(CONDA_PREFIX, 'bin')}"
os.environ["PDAL_DRIVER_PATH"] = os.path.join(CONDA_PREFIX, 'Library', 'bin')
os.environ["GDAL_DRIVER_PATH"] = os.path.join(CONDA_PREFIX, 'Library', 'bin', 'gdalplugins')
os.environ["PROJ_NETWORK"] = "ON"
os.environ["PROJ_LIB"] = os.path.join(CONDA_PREFIX, 'Library', 'share', 'proj')
os.environ["GDAL_DATA"] = os.path.join(CONDA_PREFIX, 'Library', 'share', 'gdal')
import arcpy
import dataclasses
import math
import numpy as np
import codem
import vcd


class Toolbox(object):
    def __init__(self):
        """Define the toolbox (the name of the toolbox is the name of the
        .pyt file)."""
        self.label = "3D Data Co-Registration"
        self.alias = "3d_registration"

        # List of tool classes associated with this toolbox
        self.tools = [Register_MultiType, Volumetric_Change_Detection]

class Register_MultiType(object):
    def __init__(self):
        """Define the tool (tool name is the name of the class)."""
        self.label = "Register Multi-Type"
        self.description = "Co-Register 3D Spatial Data"
        self.canRunInBackground = False

    def getParameterInfo(self):
        """Define parameter definitions"""
        fnd = arcpy.Parameter(
            displayName="Foundation Data File",
            name="foundation_file",
            datatype=["DEFile","DELasDataset","GPLasDatasetLayer","GPRasterLayer"],
            parameterType="Required",
            direction="Input",
        )
        aoi = arcpy.Parameter(
            displayName="Area of Interest (AOI) Data File",
            name="aoi_file",
            datatype=["DEFile","DELasDataset","GPLasDatasetLayer","GPRasterLayer"],
            parameterType="Required",
            direction="Input",
        )

        min = arcpy.Parameter(
            displayName="Minimum Resolution (m)",
            name="min_resolution",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
            category="Resolution",
        )

        dss = arcpy.Parameter(
            displayName="DSM Registration",
            name="dsm_solve_scale",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input",
            category="Solve Scale",
        )
        iss = arcpy.Parameter(
            displayName="ICP Registration",
            name="icp_solve_scale",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input",
            category="Solve Scale",
        )

        dsf = arcpy.Parameter(
            displayName="Normalization Strong Filter Size (m)",
            name="dsm_strong_filter",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
            category="DSM Registration Options",
        )
        dwf = arcpy.Parameter(
            displayName="Normalization Weak Filter Size (m)",
            name="dsm_weak_filter",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
            category="DSM Registration Options",
        )
        dat = arcpy.Parameter(
            displayName="AKAZE Detection Threshold",
            name="dsm_akaze_threshold",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
            category="DSM Registration Options",
        )
        dlr = arcpy.Parameter(
            displayName="Lowe's Ratio",
            name="dsm_lowes_ratio",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
            category="DSM Registration Options",
        )
        drmi = arcpy.Parameter(
            displayName="RANSAC Maximum Iterations",
            name="dsm_ransac_max_iter",
            datatype="GPLong",
            parameterType="Required",
            direction="Input",
            category="DSM Registration Options",
        )
        drt = arcpy.Parameter(
            displayName="RANSAC Error Threshold (m)",
            name="dsm_ransac_threshold",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
            category="DSM Registration Options",
        )

        imi = arcpy.Parameter(
            displayName="ICP Maximum Iterations",
            name="icp_max_iter",
            datatype="GPLong",
            parameterType="Required",
            direction="Input",
            category="ICP Registration Options",
        )
        iat = arcpy.Parameter(
            displayName="ICP Angle Threshold (degrees)",
            name="icp_angle_threshold",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
            category="ICP Registration Options",
        )
        idt = arcpy.Parameter(
            displayName="ICP Distance Threshold (m)",
            name="icp_distance_threshold",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
            category="ICP Registration Options",
        )
        irt = arcpy.Parameter(
            displayName="ICP RMSE Relative Change Threshold",
            name="icp_rmse_threshold",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
            category="ICP Registration Options",
        )
        ir = arcpy.Parameter(
            displayName="Robust ICP",
            name="icp_robust",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input",
            category="ICP Registration Options",
        )
        output = arcpy.Parameter(
            displayName="Registered File",
            name="output_file",
            datatype="DELasDataset",
            parameterType="Derived",
            direction="Output",
        )

        # Minimum pipeline resolution
        min.value = 1.0
        min.filter.type = "Range"
        min.filter.list = [0.01, 100]

        # DSM registration - solve scale option
        dss.value = True
        # ICP registration - solve scale option
        iss.value = True
        # DSM normalization strong filter size
        dsf.value = 10.0
        dsf.filter.type = "Range"
        dsf.filter.list = [0.01, 1000]
        # DSM normalization weak filter size
        dwf.value = 1.0
        dwf.filter.type = "Range"
        dwf.filter.list = [0.01, 1000]
        # AKAZE feature detection threshold
        dat.value = 0.0001
        dat.filter.type = "Range"
        dat.filter.list = [0.0000001, 1]
        # Lowe's ratio
        dlr.value = 0.9
        dlr.filter.type = "Range"
        dlr.filter.list = [0.01, 0.99]
        # Maximum RANSAC iterations in feature matching
        drmi.value = 10000
        drmi.filter.type = "Range"
        drmi.filter.list = [1, 1000000]
        # RANSAC feature location transformation error threshold
        drt.value = 10
        drt.filter.type = "Range"
        drt.filter.list = [0.01, 100]

        # Maximum ICP iterations
        imi.value = 100
        imi.filter.type = "Range"
        imi.filter.list = [1, 1000]
        # ICP convergence threshold - minimum angle change
        iat.value = 0.001
        iat.filter.type = "Range"
        iat.filter.list = [0.00001, 10]
        # ICP convergence threshold - minimum distance change
        idt.value = 0.001
        idt.filter.type = "Range"
        idt.filter.list = [0.00001, 10]
        # ICP convergence threshold - minimum relative change in RMSE
        irt.value = 0.0001
        irt.filter.type = "Range"
        irt.filter.list = [0.0000001, 1]
        # Robust ICP option
        ir.value = True

        params = [
            fnd,  # 0
            aoi,  # 1
            min,  # 2
            dss,  # 3
            iss,  # 4
            dsf,  # 5
            dwf,  # 6
            dat,  # 7
            dlr,  # 8
            drmi,  # 9
            drt,  # 10
            imi,  # 11
            iat,  # 12
            idt,  # 13
            irt,  # 14
            ir,  # 15
            output # 16
        ]
        return params

    def isLicensed(self):
        """Set whether tool is licensed to execute."""
        return True

    def updateParameters(self, parameters):
        """Modify the values and properties of parameters before internal
        validation is performed.  This method is called whenever a parameter
        has been changed."""
        return

    def updateMessages(self, parameters):
        """Modify the messages created by internal validation for each tool
        parameter.  This method is called after internal validation."""

        # Strong and weak filter size check
        if (
            parameters[5].value
            and parameters[6].value
            and parameters[5].value <= parameters[6].value
        ):
            parameters[5].setErrorMessage(
                "Strong filter size must be larger than weak filter size"
            )
            parameters[6].setErrorMessage(
                "Weak filter size must be smaller than large filter size"
            )

        # Check if input DEMs have equal X and Y cell size values

        # first make sure both params are input
        if parameters[0].value and parameters[1].value:
            # get path
            fnd_full_path = os.fsdecode(f"{self.getLayerPath(parameters[0].valueAsText)}").replace(
                os.sep, "/"
            )
            aoi_full_path = os.fsdecode(f"{self.getLayerPath(parameters[1].valueAsText)}").replace(
                os.sep, "/"
            )

            inputs_list = [fnd_full_path, aoi_full_path]
            # check for both FND and AOI
            for index, input_file in enumerate(inputs_list):
                # analysis can only be done with raster/DEM input
                if os.path.splitext(input_file)[-1] in {".tif", ".tiff"}:
                    # see number of bands in raster (Valid DEMs only have 1)
                    raster_description = arcpy.Describe(input_file)

                    # set warning (tool can still be run) if more than one band
                    if raster_description.bandCount != 1:
                        parameters[index].setWarningMessage(
                            "Warning: Input DEM has more than one band in "
                            f"{os.path.basename(input_file)}. "
                            "The tool will not run properly with the "
                            "input data as is. Consider regenerating input DEM"
                        )

                    # need to access detail of Band1 (or only band for DEMs)

                    # first get band name
                    arcpy.env.workspace = input_file
                    bands_list = arcpy.ListRasters()
                    # join only band to get band description
                    # Refer to code sample for accessing Raster Band Properties: https://pro.arcgis.com/en/pro-app/2.9/arcpy/functions/raster-band-properties.htm
                    band_description = arcpy.Describe(
                        os.path.join(input_file, bands_list[0])
                    )

                    # CODEM requires raster cell sizes to be within 1e-5
                    # however arcpy determines different cells sizes from gdal
                    # so we apply a more forgiving tolerance as a smoke-screen check for ArcGIS users
                    if not math.isclose(
                        band_description.meanCellHeight,
                        band_description.meanCellWidth,
                        rel_tol=1e-2,
                    ):
                        parameters[index].setErrorMessage(
                            "Error: X and Y cell sizes are not equal in "
                            f"{os.path.basename(input_file)}. "
                            "The tool will not run with the input data as is. "
                            "Consider reprojecting input DEM"
                            f" X = {band_description.meanCellWidth}, Y = {band_description.meanCellHeight}"
                        )

        #check that correct input 3D data types are being used ("las", "laz", "bpf", "ply", "obj", "tif", "tiff")
        acceptable_data_list = ["las", "laz", "bpf", "ply", "obj", "tif", "tiff"]
        if parameters[0].value:

            #getting the proper file extension, accounting for case where raster band appears as the last item in file path, such as C:/my/path/file.tif/band_1
            #also accounts for when layer title does not end with file extension
            if self.getFileExtension(parameters[0].valueAsText) not in acceptable_data_list:
                fnd_full_path = os.fsdecode(f"{self.getLayerPath(parameters[0].valueAsText)}").replace(os.sep, "/")
                fnd_file_split = os.path.split(fnd_full_path)
                if self.getFileExtension(fnd_file_split[0]) not in acceptable_data_list:
                    parameters[0].setErrorMessage(
                        "File not able to be coregistered."
                        f" Acceptable file types are {acceptable_data_list}"
                        f" This file is a {self.getFileExtension(fnd_file_split[0])}"
                    )

            #check that data is projected, as data with only a GCS won't work
            spatial_ref = arcpy.Describe(parameters[0]).spatialReference
            if spatial_ref.type == "Geographic":
                parameters[0].setErrorMessage(
                    "This dataset cannot be successfully coregistered as it is not projected. Please project and try again."
                )
            
                
        if parameters[1].value:

            #getting the proper file extension, accounting for case where raster band appears as the last item in file path, such as C:/my/path/file.tif/band_1
            #also accounts for when layer title does not end with file extensio
            if self.getFileExtension(parameters[1].valueAsText) not in acceptable_data_list:
                aoi_full_path = os.fsdecode(f"{self.getLayerPath(parameters[1].valueAsText)}").replace(os.sep, "/")
                aoi_file_split = os.path.split(aoi_full_path)
                if self.getFileExtension(aoi_file_split[1]) not in acceptable_data_list:
                    parameters[1].setErrorMessage(
                        "File not able to be coregistered."
                        f" Acceptable file types are {acceptable_data_list}"
                        f" This file is a {self.getFileExtension(aoi_file_split[1])}"
                    )

            #check that data is projected, as data with only a GCS won't work
            spatial_ref = arcpy.Describe(parameters[1]).spatialReference
            if spatial_ref.type == "Geographic":
                parameters[1].setErrorMessage(
                    "This dataset cannot be successfully coregistered as it is not projected. Please project and try again."
                )

        return

    def getLayerPath(self, layer):
        if not os.path.exists(layer):
        # we are working with an ArcGIS scene layer and not a file:
            desc = arcpy.Describe(layer)
            layer = os.path.join(desc.path, layer)
        return layer

    def getFileExtension(self, layer):
        desc = arcpy.Describe(layer)
        return desc.extension

    def execute(self, parameters, messages):
        """The source code of the tool."""

        fnd_full_path = os.fsdecode(f"{self.getLayerPath(parameters[0].valueAsText)}").replace(os.sep, "/")
        aoi_full_path = os.fsdecode(f"{self.getLayerPath(parameters[1].valueAsText)}").replace(os.sep, "/")
        aoi_file_extension = os.path.splitext(aoi_full_path)[-1]

        dsm_filetypes = codem.lib.resources.dsm_filetypes
        pcloud_filetypes = codem.lib.resources.pcloud_filetypes
        mesh_filetypes = codem.lib.resources.mesh_filetypes

        # create mapping of codem supported inputs vs. ArcGIS supported outputs
        mapping = {dsm_filetype: ".tif" for dsm_filetype in dsm_filetypes}
        for pcloud_filetype in pcloud_filetypes:
            mapping[pcloud_filetype] = ".las"
        for mesh_filetype in mesh_filetypes:
            mapping[mesh_filetype] = ".obj"

        arcpy.SetProgressor("step", "Registering AOI to Foundation", 0, 5)

        kwargs = {
            parameter.name.upper(): parameter.value for parameter in parameters[2:15]
        }
        codem_run_config = codem.CodemRunConfig(fnd_full_path, aoi_full_path, **kwargs)
        config = dataclasses.asdict(codem_run_config)

        #Add output to details pane with parameters and their values
        arcpy.AddMessage("=============PARAMETERS=============")
        for parameter, value in config.items():
            arcpy.AddMessage(f"{parameter}={value}")
        arcpy.SetProgressorLabel("Step 1/4: Prepping AOI and Foundation Data")
        arcpy.SetProgressorPosition()
        arcpy.AddMessage("=============PREPROCESSING DATA=============")
        fnd_obj, aoi_obj = codem.preprocess(config)

        fnd_obj.prep()
        aoi_obj.prep()

        if fnd_obj.units:
            arcpy.AddMessage(f"Linear unit for Foundation-{fnd_obj.type.upper()} detected as {fnd_obj.units}")
        else:
            arcpy.AddMessage(f"Linear unit for Foundation-{fnd_obj.type.upper()} not detected -> "
                    "meters assumed")
        arcpy.AddMessage(f"Calculated native resolution of {fnd_obj.type.upper()} as: "
            f"{fnd_obj.native_resolution:.1f} meters")

        if aoi_obj.units:
            arcpy.AddMessage(f"Linear unit for AOI-{aoi_obj.type.upper()} detected as {aoi_obj.units}")
        else:
            arcpy.AddMessage(f"Linear unit for Foundation-{aoi_obj.type.upper()} not detected -> "
                    "meters assumed")
        arcpy.AddMessage(f"Calculated native resolution of {aoi_obj.type.upper()} as: "
            f"{aoi_obj.native_resolution:.1f} meters")

        arcpy.AddMessage(f"Preparing Foundation {fnd_obj.type.upper()} for registration.")
        arcpy.AddMessage(f"Extracting DSM from FND {fnd_obj.type.upper()} with resolution of: "
            f"{fnd_obj.resolution} meters")

        arcpy.AddMessage(f"Preparing AOI {aoi_obj.type.upper()} for registration.")
        arcpy.AddMessage(f"Extracting DSM from AOI {aoi_obj.type.upper()} with resolution of: "
            f"{aoi_obj.resolution} meters")
        #No resampling required message?
        arcpy.AddMessage(f"Registration resolution has been set to: "
            f"{fnd_obj.resolution} meters")

        arcpy.SetProgressorLabel("Step 2/4: Solving Coarse Registration")
        arcpy.SetProgressorPosition()
        arcpy.AddMessage("=============BEGINNING COARSE REGISTRATION=============")

        dsm_reg = codem.coarse_registration(fnd_obj, aoi_obj, config)
        arcpy.AddMessage("Solving DSM feature registration.")
        arcpy.AddMessage(f"{len(dsm_reg.fnd_kp)} keypoints detected in foundation.")
        arcpy.AddMessage(f"{len(dsm_reg.aoi_kp)} keypoints detected in AOI.")
        arcpy.AddMessage(f"{len(dsm_reg.putative_matches)} putative keypoint matches found.")
        arcpy.AddMessage(f"{np.sum(dsm_reg.inliers)} keypoint matches found.")

        feature_viz = os.path.join(dsm_reg.config["OUTPUT_DIR"], "dsm_feature_matches.png")
        arcpy.AddMessage(f"Saving DSM feature match visualization to: {feature_viz}")

        registration_parameters = os.path.join(dsm_reg.config["OUTPUT_DIR"], "registration.txt")
        arcpy.AddMessage(f"Saving DSM feature registration parameters to: {registration_parameters}")

        arcpy.SetProgressorLabel("Step 3/4: Solving Fine Registration")
        arcpy.SetProgressorPosition()
        arcpy.AddMessage("=============BEGINNING FINE REGISTRATION=============")



        icp_reg = codem.fine_registration(fnd_obj, aoi_obj, dsm_reg, config)
        arcpy.AddMessage("Solving ICP registration.")
        # ICP Converge statements - need support to interpret RMSE math
        # For number of ICP iterations, need to create an 'iterator' or 'i+1' self. variable to output here
        icp_parameters = os.path.join(icp_reg.config["OUTPUT_DIR"], "registration.txt")

        arcpy.AddMessage(f"Saving ICP registration parameters to {icp_parameters}")


        arcpy.SetProgressorLabel("Step 4/4: Applying Registration to AOI Data")
        arcpy.SetProgressorPosition()
        arcpy.AddMessage("=============APPLYING REGISTRATION=============")

        reg_file = codem.apply_registration(
            fnd_obj, aoi_obj, icp_reg, config, output_format=mapping[aoi_file_extension].lstrip(".")
        )
        arcpy.AddMessage(f"Registration has been applied to AOI-DSM and saved to: {reg_file}")
        parameters[16] = reg_file
        arcpy.AddMessage(parameters[16])
        if not os.path.exists(reg_file):
            arcpy.AddError(f"Registration file '{reg_file}' not generated")
            return None
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        activeMap = aprx.activeMap
        arcpy.env.addOutputsToMap = True
        if activeMap is None:
            arcpy.AddWarning("activeMap is None")
        elif aoi_file_extension in mesh_filetypes:
            arcpy.AddWarning(
                f"File type {aoi_file_extension} cannot be visualized in ArcGIS Pro. "
                "Consider converting AOI or visualizing in other software."
            )
        else:
            if aoi_file_extension == '.tif':
                arcpy.management.CalculateStatistics(reg_file, 1, 1, [], "OVERWRITE", r"in_memory\feature_set1")
                arcpy.AddMessage(f"Raster Statistics calculated for {reg_file}")
            activeMap.addDataFromPath(reg_file)
            arcpy.AddMessage(f"ActiveMap added {aoi_file_extension} file")
        return None

class Volumetric_Change_Detection(object):
    def __init__(self):
        """Define the tool (tool name is the name of the class)."""
        self.label = "Volumetric Change Detection"
        self.description = "Developed by Brad Chambers and Howard Butler"
        self.canRunInBackground = False

    def getParameterInfo(self):
        """Define parameter definitions"""
        before = arcpy.Parameter(
            displayName="Before LiDAR Scan",
            name="before",
            datatype=["DEFile","DELasDataset","GPLasDatasetLayer"],
            parameterType="Required",
            direction="Input",
        )

        after = arcpy.Parameter(
            displayName="After LiDAR Scan",
            name="after",
            datatype=["DEFile","DELasDataset","GPLasDatasetLayer"],
            parameterType="Required",
            direction="Input",
        )
        spacing = arcpy.Parameter(
            displayName="Spacing Override",
            name="spacing",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Optional Parameters"
        )

        ground_height = arcpy.Parameter(
            displayName="Ground Height",
            name="groundheight",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Optional Parameters"
        )

        resolution = arcpy.Parameter(
            displayName="Resolution",
            name="resolution",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Optional Parameters"
        )

        verbose = arcpy.Parameter(
            displayName="Verbose",
            name="verbose",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
            category="Optional Parameters"

        )

        min_points = arcpy.Parameter(
            displayName="Minimum Points",
            name="min_points",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Optional Parameters"
        )

        cluster_tolerance = arcpy.Parameter(
            displayName="Cluster Tolerance",
            name="cluster_tolerance",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Optional Parameters"
        )
        grnd_output = arcpy.Parameter(
            displayName="Change Detection Mesh (Ground)",
            name="output_file_grnd",
            datatype=["DEFile","DELasDataset","GPLasDatasetLayer","GPRasterLayer"],
            parameterType="Derived",
            direction="Output",
        )
        nongrnd_output = arcpy.Parameter(
            displayName="Change Detection Mesh (Non-Ground)",
            name="output_file_nongrnd",
            datatype="DEShapefile",
            parameterType="Derived",
            direction="Output",
        )

        spacing.value = 0.43
        ground_height.value = 1.0
        resolution.value = 1.0
        min_points.value = 30.0
        cluster_tolerance.value = 2.0

        #            0     1      2         3            4         5        6              7           8              9
        params = [before,after,spacing,ground_height,resolution,verbose,min_points,cluster_tolerance,grnd_output,nongrnd_output]

        spacing.value = 0.43
        ground_height.value = 1.0
        resolution.value = 1.0

        return params

    def isLicensed(self):
        """Set whether tool is licensed to execute."""
        return True

    def updateParameters(self, parameters):
        """Modify the values and properties of parameters before internal
        validation is performed.  This method is called whenever a parameter
        has been changed."""
        return

    def updateMessages(self, parameters):
        """Modify the messages created by internal validation for each tool
        parameter.  This method is called after internal validation."""
        return

    def getLayerPath(self, layer):
            if not os.path.exists(layer):
            # we are working with an ArcGIS scene layer and not a file:
                desc = arcpy.Describe(layer)
                layer = os.path.join(desc.path, layer)
            return layer


    def execute(self, parameters, messages):
        """The source code of the tool."""

        before_full_path = os.fsdecode(f"{self.getLayerPath(parameters[0].valueAsText)}").replace(os.sep, "/")
        after_full_path = os.fsdecode(f"{self.getLayerPath(parameters[1].valueAsText)}").replace(os.sep, "/")
        arcpy.SetProgressor("step", "Calculating Volumetric Change", 0, 5)


        kwargs = {
            parameter.name.upper(): parameter.value for parameter in parameters[2:7]
        }

        vcd_run_config = vcd.VcdRunConfig(before_full_path, after_full_path, **kwargs)
        config = dataclasses.asdict(vcd_run_config)
        arcpy.AddMessage("====== PARAMETERS ======")
        for key in config:
            arcpy.AddMessage(f"{key} = {config[key]}")

        arcpy.SetProgressorLabel("Step 1/4: Prepping Before and After Data")
        arcpy.SetProgressorPosition()
        arcpy.AddMessage("====== PREPROCESSING DATA ======")
        log = codem.log.Log(config)
        config['log']=log

        arcpy.AddMessage("====== FILTERING 'BEFORE' DATA ======")
        before = vcd.PointCloud(config, "BEFORE")

        arcpy.AddMessage("====== FILTERING 'AFTER' DATA ======")
        after = vcd.PointCloud(config, "AFTER")

        arcpy.AddMessage("====== COMPUTING INDEXES FOR COMPARISON ======")
        v = vcd.VCD(before,after)
        v.compute_indexes()

        arcpy.SetProgressorLabel("Step 2/4: Extracting Differences")
        arcpy.SetProgressorPosition()
        arcpy.AddMessage("====== EXTRACTING DIFFERENCES ======")
        v.make_products()

        arcpy.SetProgressorLabel("Step 3/4: Creating Clusters")
        arcpy.SetProgressorPosition()
        arcpy.AddMessage("====== CLUSTERING ======")
        v.cluster()


        arcpy.SetProgressorLabel("Step 4/4: Generating Raster and Mesh Products")
        arcpy.SetProgressorPosition()
        arcpy.AddMessage("====== RASTERIZING PRODUCTS ======")
        v.rasterize()

        arcpy.AddMessage("====== MESHING PRODUCTS ======")
        m = vcd.Mesh(v)
        m.write("non-ground", m.cluster(v.ng_clusters))
        m.write("ground", m.cluster(v.ground_clusters))
        v.save()

        arcpy.AddMessage(vcd_run_config.OUTPUT_DIR)
        ground_file = os.path.join(vcd_run_config.OUTPUT_DIR,"meshes","ground.shp")
        nonground_file = os.path.join(vcd_run_config.OUTPUT_DIR,"meshes","non-ground.shp")
        parameters[8]=ground_file
        parameters[9]=nonground_file

        if not os.path.exists(ground_file):
            arcpy.AddError(f"Ground file '{ground_file}' not generated")
            return None
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        activeMap = aprx.activeMap
        arcpy.env.addOutputsToMap = True
        if activeMap is None:
            arcpy.AddWarning("activeMap is None")
        activeMap.addDataFromPath(ground_file)
        activeMap.addDataFromPath(nonground_file)
        arcpy.AddMessage("ActiveMap added ground and nonground multipatch files")
        return
