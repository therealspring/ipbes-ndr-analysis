"""Script to mosaic NDR results into single rasters."""
import itertools
import time
import sys
import logging
import os
import multiprocessing

import numpy
from osgeo import gdal
from osgeo import osr
import pygeoprocessing
import taskgraph

# set a 1GB limit for the cache
gdal.SetCacheMax(2**30)

WORKSPACE_DIR = 'mosaic_workspace'
N_WORKERS = min(8, multiprocessing.cpu_count())
TASKGRAPH_UPDATE_INTERVAL = 5.0

logging.basicConfig(
    level=logging.DEBUG,
    format=(
        '%(asctime)s (%(relativeCreated)d) %(levelname)s %(name)s'
        ' [%(funcName)s:%(lineno)d] %(message)s'),
    stream=sys.stdout)
LOGGER = logging.getLogger(__name__)

NDR_DIRECTORY = os.path.join(
    'workspace_ipbes_ndr', 'watershed_processing')

# degree is 110570 at the Equator and we want 300m pixels
MOSAIC_DEGREE_CELL_SIZE = 300.0 / 110570
RASTER_SUFFIXES_TO_AGGREGATE = (
    'worldclim_esa_2015_n_export.tif',
    'worldclim_esa_2015_modified_load.tif',
    #'worldclim_2015_n_export.tif',
    #'worldclim_2050_ssp1_n_export.tif',
    #'worldclim_2050_ssp3_n_export.tif',
    #'worldclim_2050_ssp5_n_export.tif',
    #'SSP1_aligned_ndr.tif',
    #'SSP3_aligned_ndr.tif',
    #'SSP5_aligned_ndr.tif',
    #'2015_aligned_ndr.tif',
    #'worldclim_2015_modified_load.tif',
    #'worldclim_2050_ssp1_modified_load.tif',
    #'worldclim_2050_ssp3_modified_load.tif',
    #'worldclim_2050_ssp5_modified_load.tif',
    )

_WGS84_SRS = osr.SpatialReference()
_WGS84_SRS.ImportFromEPSG(4326)
WSGS84_WKT = _WGS84_SRS.ExportToWkt()


def main():
    """Entry point."""
    task_graph = taskgraph.TaskGraph(
        WORKSPACE_DIR, N_WORKERS, TASKGRAPH_UPDATE_INTERVAL)
    task_graph.join()  # get taskgrpah started

    try:
        os.makedirs(WORKSPACE_DIR)
    except OSError:
        pass

    global_raster_task_path_map = {}
    LOGGER.debug("gathering directory list")
    leaf_directory_list = (
        (dirpath, filenames) for (dirpath, dirnames, filenames) in os.walk(
            NDR_DIRECTORY) if not dirnames)

    # peek at first element
    sample_dirpath, sample_filenames = next(leaf_directory_list)

    for raster_suffix in RASTER_SUFFIXES_TO_AGGREGATE:
        try:
            base_raster_path = next(iter(
                (os.path.join(sample_dirpath, file_path)
                    for file_path in sample_filenames
                    if file_path.endswith(raster_suffix))))
        except StopIteration:
            raise ValueError(
                "Expected to find %s in %s but not found" % (
                    raster_suffix, sample_dirpath))

        base_raster_info = pygeoprocessing.get_raster_info(base_raster_path)
        target_raster_path = os.path.join(WORKSPACE_DIR, raster_suffix)
        target_token_complete_path = f'''{
            os.path.splitext(target_raster_path)[0]}_{
                MOSAIC_DEGREE_CELL_SIZE}.TOKEN'''
        LOGGER.debug(target_raster_path)
        make_empty_raster_task = task_graph.add_task(
            func=make_empty_wgs84_raster,
            args=(
                MOSAIC_DEGREE_CELL_SIZE, base_raster_info['nodata'][0],
                base_raster_info['datatype'], target_raster_path,
                target_token_complete_path),
            ignore_path_list=[target_raster_path],
            target_path_list=[target_token_complete_path],
            task_name=f'create empty global {raster_suffix}')
        global_raster_task_path_map[raster_suffix] = (
            make_empty_raster_task, target_raster_path)
    LOGGER.info("found all the raster suffixes in %s", sample_dirpath)

    for raster_suffix in RASTER_SUFFIXES_TO_AGGREGATE:
        previous_project_task_list = []
        leaf_directory_list = (
            (dirpath, filenames) for (dirpath, dirnames, filenames) in os.walk(
                NDR_DIRECTORY) if not dirnames)
        for dirpath, filenames in leaf_directory_list:
            try:
                base_raster_path = next(iter(
                    (os.path.join(dirpath, file_path)
                        for file_path in filenames
                        if file_path.endswith(raster_suffix))))
            except StopIteration:
                raise RuntimeError(
                    "Expected to find %s in %s but not found %s" % (
                        raster_suffix, dirpath, (dirpath, filenames)))

            target_wgs84_raster_path = f'''{
                os.path.splitext(base_raster_path)[0]}_wgs84.tif'''
            wgs84_project_task = task_graph.add_task(
                func=pygeoprocessing.warp_raster,
                args=(
                    base_raster_path,
                    (MOSAIC_DEGREE_CELL_SIZE, -MOSAIC_DEGREE_CELL_SIZE),
                    target_wgs84_raster_path, 'near'),
                kwargs={'target_sr_wkt': WSGS84_WKT},
                target_path_list=[target_wgs84_raster_path],
                dependent_task_list=[
                    global_raster_task_path_map[raster_suffix][0]],
                task_name=f'''wgs84 project {
                    os.path.basename(base_raster_path)}''')

            mosaic_complete_token_path = f'''{
                os.path.splitext(target_wgs84_raster_path)[0]}.MOSAICKED'''
            mosiac_task = task_graph.add_task(
                func=mosaic_base_into_target,
                args=(
                    target_wgs84_raster_path,
                    global_raster_task_path_map[raster_suffix][1],
                    mosaic_complete_token_path),
                ignore_path_list=[
                    global_raster_task_path_map[raster_suffix][1]],
                target_path_list=[mosaic_complete_token_path],
                dependent_task_list=(
                    [wgs84_project_task]+previous_project_task_list),
                task_name=f'''mosiac {
                    os.path.basename(target_wgs84_raster_path)}''')
            # this ensures that a mosiac will happen one at a time
            previous_project_task_list = [mosiac_task]

    task_graph.join()

    for _, base_path in global_raster_task_path_map.values():
        target_path = f'{os.path.splitext(base_path)[0]}_compressed.tif'
        LOGGER.info(f'starting {base_path} to {target_path}')
        task_graph.add_task(
            func=compress_to,
            args=(base_path, 'near', target_path),
            target_path_list=[target_path],
            task_name=f'''compress {base_path}''')

    task_graph.join()
    task_graph.close()


def compress_to(base_raster_path, resample_method, target_path):
    """Compress base to target using resample method for overviews."""
    gtiff_driver = gdal.GetDriverByName('GTiff')
    base_raster = gdal.OpenEx(base_raster_path, gdal.OF_RASTER)
    LOGGER.info('compress %s to %s' % (base_raster_path, target_path))
    gtiff_driver.CreateCopy(
        target_path, base_raster, options=(
            'TILED=YES', 'BIGTIFF=YES', 'COMPRESS=LZW',
            'BLOCKXSIZE=256', 'BLOCKYSIZE=256'))
    base_raster = None
    min_dimension = min(
        pygeoprocessing.get_raster_info(target_path)['raster_size'])
    LOGGER.info(f"min min_dimension {min_dimension}")
    raster_copy = gdal.OpenEx(target_path, gdal.OF_RASTER)

    overview_levels = []
    current_level = 2
    while True:
        if min_dimension // current_level == 0:
            break
        overview_levels.append(current_level)
        current_level *= 2
    LOGGER.info(f'level list: {overview_levels}')
    gdal.SetConfigOption('COMPRESS_OVERVIEW', 'LZW')
    raster_copy.BuildOverviews(
        resample_method, overview_levels, callback=_make_logger_callback(
            f'build overview for {os.path.basename(target_path)} '
            '%.2f%% complete'))


def mosaic_base_into_target(
        base_raster_path, target_raster_path, target_token_complete_path):
    """Copy valid parts of base to target w/r/t correct georeference.

    Parameters:
        base_raster_path (str): a raster with the same cell size,
            coordinate system, and nodata as `target_raster_path`.
        target_raster_path (str): a raster that already exists on disk that
            after this call will contain the non-nodata parts of
            `base_raster_path` that geographically overlap with the target.
        target_token_complete_path (str): this file is created if the
            mosaic to target is successful. Useful for taskgraph task
            scheduling.

    Returns:
        None.

    """
    target_raster = gdal.OpenEx(
        target_raster_path, gdal.OF_RASTER | gdal.GA_Update)
    target_band = target_raster.GetRasterBand(1)
    target_raster_info = pygeoprocessing.get_raster_info(target_raster_path)
    target_nodata = target_raster_info['nodata'][0]
    base_raster_info = pygeoprocessing.get_raster_info(base_raster_path)
    target_gt = target_raster_info['geotransform']
    base_gt = base_raster_info['geotransform']

    target_x_off = int((base_gt[0] - target_gt[0]) / target_gt[1])
    target_y_off = int((base_gt[3] - target_gt[3]) / target_gt[5])

    for offset_dict, band_data in pygeoprocessing.iterblocks(
            (base_raster_path, 1)):
        target_block = target_band.ReadAsArray(
            xoff=offset_dict['xoff']+target_x_off,
            yoff=offset_dict['yoff']+target_y_off,
            win_xsize=offset_dict['win_xsize'],
            win_ysize=offset_dict['win_ysize'])
        valid_mask = numpy.isclose(target_block, target_nodata)
        target_block[valid_mask] = band_data[valid_mask]
        target_band.WriteArray(
            target_block,
            xoff=offset_dict['xoff']+target_x_off,
            yoff=offset_dict['yoff']+target_y_off)
    target_band.FlushCache()
    target_band = None
    target_raster = None

    with open(target_token_complete_path, 'w') as token_file:
        token_file.write('complete!')


def make_empty_wgs84_raster(
        cell_size, nodata_value, target_datatype, target_raster_path,
        target_token_complete_path):
    """Make a big empty raster in WGS84 projection.

    Parameters:
        cell_size (float): this is the desired cell size in WSG84 degree
            units.
        nodata_value (float): desired nodata avlue of target raster
        target_datatype (gdal enumerated type): desired target datatype.
        target_raster_path (str): this is the target raster that will cover
            [-180, 180), [90, -90) with cell size units with y direction being
            negative.
        target_token_complete_path (str): this file is created if the
            mosaic to target is successful. Useful for taskgraph task
            scheduling.

    Returns:
        None.

    """
    gtiff_driver = gdal.GetDriverByName('GTiff')
    try:
        os.makedirs(os.path.dirname(target_raster_path))
    except OSError:
        pass

    n_cols = int(360.0 / cell_size)
    n_rows = int(180.0 / cell_size)

    geotransform = (-180.0, cell_size, 0.0, 90.0, 0, -cell_size)

    target_raster = gtiff_driver.Create(
        target_raster_path, n_cols, n_rows, 1, target_datatype,
        options=(
            'TILED=YES', 'BIGTIFF=YES', 'BLOCKXSIZE=256', 'BLOCKYSIZE=256'))
    target_raster.SetProjection(WSGS84_WKT)
    target_raster.SetGeoTransform(geotransform)
    target_band = target_raster.GetRasterBand(1)
    target_band.SetNoDataValue(nodata_value)
    LOGGER.debug(f"filling {target_raster_path} with {nodata_value}")
    target_band.Fill(nodata_value)
    target_band.FlushCache()
    target_band = None
    target_raster = None

    target_raster = gdal.OpenEx(target_raster_path, gdal.OF_RASTER)
    if target_raster:
        with open(target_token_complete_path, 'w') as target_token_file:
            target_token_file.write('complete!')


def _make_logger_callback(message):
    """Build a timed logger callback that prints ``message`` replaced.

    Parameters:
        message (string): a string that expects 2 placement %% variables,
            first for % complete from ``df_complete``, second from
            ``p_progress_arg[0]``.

    Returns:
        Function with signature:
            logger_callback(df_complete, psz_message, p_progress_arg)

    """
    def logger_callback(df_complete, _, p_progress_arg):
        """Argument names come from the GDAL API for callbacks."""
        try:
            current_time = time.time()
            if ((current_time - logger_callback.last_time) > 5.0 or
                    (df_complete == 1.0 and
                     logger_callback.total_time >= 5.0)):
                # In some multiprocess applications I was encountering a
                # ``p_progress_arg`` of None. This is unexpected and I suspect
                # was an issue for some kind of GDAL race condition. So I'm
                # guarding against it here and reporting an appropriate log
                # if it occurs.
                if p_progress_arg:
                    LOGGER.info(message, df_complete * 100, p_progress_arg[0])
                else:
                    LOGGER.info(
                        'p_progress_arg is None df_complete: %s, message: %s',
                        df_complete, message)
                logger_callback.last_time = current_time
                logger_callback.total_time += current_time
        except AttributeError:
            logger_callback.last_time = time.time()
            logger_callback.total_time = 0.0

    return logger_callback


if __name__ == '__main__':
    main()
