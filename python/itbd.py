#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
itbd.py

Main module for the Iceberg Tracking Beacon Database (ITDB).

Defines the class 'Track' containing an individual iceberg track along with methods and properties.
Defines the class 'Models' containing beacon model specifications, used for cleaning
Defines the class 'Meta' containing the database metadata

Includes a workflow for cleaning a beacon track using these classes

"""

import os
import sys
import argparse
import logging
from pathlib import Path
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString
import numpy as np
import pyproj
from collections import namedtuple
import json
import copy

# this functionality is in other modules.  Import
import track_readers
from track_fig import plot_temp, plot_map, plot_dist, plot_time


def nolog():
    """
    Create a logger instance that doesn't do anything.

    Used to allow logging or not in the code below

    Returns
    -------
    NoOpLogger
        A named tuple that mimics a log instance.

    """
    NoOpLogger = namedtuple(
        "NoOpLogger", ["debug", "info", "warning", "error", "critical"]
    )
    return NoOpLogger(*([lambda *args, **kwargs: None] * 5))


class Models:
    """
    Class that holds info/specifications for all beacon models.

    Currently that is the valid range (min, max) of various sensors but this could be
    expanded to hold any data related to the model


    """

    def __init__(self, model_file, logger=None):
        """
        Read spec file and create a dataframe.

        Parameters
        ----------
        model_file : str
            Full path to the model_file (*.ods or *.xls or *.xlsx).

        Returns
        -------
        model_df : pandas dataframe
            Specifications for all beacon models.
        logger: instance of logger class
            Pass a logger here if you want

        """
        if logger is None:
            logger = nolog()
        self.log = logger

        self.model_file = model_file
        try:
            df = pd.read_excel(model_file)
        except:
            pass
            self.log.error(f"Model specifications file {self.model_file} read error")
        self.df = df
        self.log.info(f"Model specifications file {self.model_file} read")


class Specs:
    """Class that holds info/specifications for a specific beacon model."""

    def __init__(self, logger=None):
        """
        Initialize class, don't load data until ready.

        Parameters
        ----------
        logger: instance of logger class
            Pass a logger here if you want

        """
        if logger is None:
            logger = nolog()
        self.log = logger

    def load_model_specs(self, model, Models):
        """
        Read the model spec record for this beacon and write track properties.

        Parameters
        ----------
        model : str
            The exact name of the beacon model
        Models : Models object
            An instance of the class Models representing a dataframe of all the model specs.

        """
        self.log.info("Reading model specifications")

        default_specs = Models.df.loc[Models.df.model == "Default"]
        beacon_specs = Models.df.loc[Models.df.model == model]

        if len(default_specs) != 1:
            self.log.error(
                f"Unkown model {model}, check spelling -or- duplicate model retrieved"
            )
            raise Exception(
                f"Unkown model {model}, check spelling -or- duplicate model retrieved"
            )
        if len(beacon_specs) != 1:
            self.log.error(
                f"Unkown model {model}, check spelling -or- duplicate model retrieved"
            )
            raise Exception(
                f"Unkown model {model}, check spelling -or- duplicate model retrieved"
            )

        # find which specs are undefined (=?) and replace with default
        beacon_specs_ind = beacon_specs.loc[:].values == "?"
        beacon_specs_ind = np.argwhere(beacon_specs_ind.flatten()).tolist()
        beacon_specs_ind = [i for row in beacon_specs_ind for i in row]
        beacon_specs.iloc[0, beacon_specs_ind] = default_specs.iloc[0, beacon_specs_ind]

        # remove a few columns that are not needed
        beacon_specs = beacon_specs.drop(columns=["notes", "model"])

        for column in beacon_specs.columns:
            setattr(self, column, beacon_specs[column].iloc[0])

        # now coerce the data to floats, satellites and loc_accuracy are really integers, so try, but skip if NA
        specs = beacon_specs.filter(regex="_max|_min")
        specs = specs.astype(float)
        try:
            specs = specs.astype({"satellites_max": int, "satellites_min": int})
        except:
            pass
        try:
            specs = specs.astype({"loc_accuracy_max": int, "loc_accuracy_min": int})
        except:
            pass
        # overwrite the properties above but with proper data types
        for column in specs.columns:
            setattr(self, column, specs[column].iloc[0])


class Meta:
    """
    Class that reads a metadata file and stores all rows in a dataframe
    """

    def __init__(self, meta_file, logger=None):
        """
        Read metadata file and create a dataframe.

        Parameters
        ----------
        meta_file : str
            Full path to the beacon metadata file. (*.xls, *.xlsx, *.ods)
        logger: instance of logger class
            Pass a logger here if you want

            Returns
            -------
            None.

        """
        if logger is None:
            logger = nolog()
        self.log = logger

        self.meta_file = meta_file

        try:
            df = pd.read_excel(meta_file)
        except:
            self.log.error(f"Failed to read {self.meta_file}, exiting... ")
            raise Exception(f"Failed to read {self.meta_file}")
        self.df = df
        self.log.info(f"Specifications file {self.meta_file} read")


class Track:
    """
    This class represents an iceberg beacon track.
    """

    def __init__(
        self,
        data_file,
        reader="standard",
        model=None,
        track_start=None,
        track_end=None,
        metadata=None,
        logger=None,
    ):
        """
        Read the track raw or standardized data.

        Note the default is to read standardized data (which should also be fully cleaned)
        If metadata is provided, that info will be used; otherwise properties
        will be set from keywords here

        Parameters
        ----------
        data_file : str
            path to the raw data file.
        reader : str, optional
            name of the reader function to use. The default is "standard".
        model : str, optional
            name of the beacon model. The default is None.
        track_start : str, optional
            Datetime to trim the start of the track. The default is None.
        track_end : str, optional
            Datetime to trim the end of the track. The default is None.
        metadata : Meta Class, optional
            An object of the meta class. The default is None.
        logger : logger instance, optional
            A logger instance to log to. The default is None.

        Returns
        -------
        None.

        """

        if logger == None:
            logger = nolog()
        self.log = logger  # a log instance is now part of the class

        self.datafile = data_file
        self.beacon_id = Path(self.datafile).stem

        self.log.info(f"~Starting to process beacon {self.beacon_id}......")

        # if metadata is not given then set properties
        if metadata == None:
            self.reader = reader
            self.model = model
            self.track_start = track_start
            self.track_end = track_end

        else:
            self.load_metadata(metadata)

        self.log.info(
            f"Reading data from {self.datafile} using the {self.reader} reader"
        )

        reader_function = getattr(track_readers, self.reader)
        self.data = reader_function(self.datafile, self.log)

        # at a minimum these cleaning steps should be taken since a track must have all 3 of these
        # Drop all rows where datetime_data, latitude or longitude is nan
        self.data.dropna(
            subset=["datetime_data", "latitude", "longitude"], inplace=True
        )

        # make sure these datetimes convert ok
        if self.track_start:
            try:
                self.track_start = pd.to_datetime(self.track_start, utc=True)
            except:
                self.log.error("Unrecognized track start format")
                raise Exception("Check track start value")
        if self.track_end:
            try:
                self.track_end = pd.to_datetime(self.track_end, utc=True)
            except:
                self.log.error("Unrecognized track end format")
                raise Exception("Check track end value")
        # This captures the data file start and end - it does not necessarily relate to the track.
        self.data_start = self.data.datetime_data.min()
        self.data_end = self.data.datetime_data.max()

        self.trackpoints = None
        self.trackline = None

        # These properties track what has been done to the track.
        self.cleaned = False
        self.sorted = False
        self.speeded = False
        self.speedlimited = False
        self.trimmed = False
        self.geoed = False

        # if the reader is standard then assume this has been done previously.
        if self.reader == "standard":
            self.cleaned = True
            self.sorted = True
            self.speeded = True
            self.speedlimited = True
            self.trimmed = True

        # generate stats
        self.stats()

        # make room for beacon specs that could be associated with this track later
        self.specs = Specs(logger=self.log)

        self.log.info(
            f"Raw data read-in with {self.observations} rows of valid data from {self.data_start} to {self.data_end}"
        )

    def stats(self):
        """
        Calculate key track properties.

        Note that this method should be run after processing steps that may affect the data

        Returns
        -------
        None.

        """

        # populate some simple properties that all tracks have
        self.beacon_id = self.data.beacon_id.iloc[0]
        self.year, self.id = self.beacon_id.split("_")
        if self.trimmed:
            duration = self.track_end - self.track_start
        else:
            duration = self.data_end - self.data_start
        self.duration = round(duration.days + duration.seconds / (24 * 60 * 60), 2)
        self.observations = len(self.data.index)

        # this only works after some processing or if the data are standard
        # note this will change when trimmed too...
        if self.sorted:
            self.latitude_start = self.data.latitude.iloc[0]
            self.longitude_start = self.data.longitude.iloc[-1]
            self.latitude_end = self.data.latitude.iloc[0]
            self.longitude_end = self.data.longitude.iloc[-1]
        else:
            self.latitude_start = None
            self.longitude_start = None
            self.latitude_end = None
            self.longitude_end = None

        if self.speeded:
            self.distance = round(self.data["distance"].sum() / 1000, 2)
        else:
            self.distance = None

    def load_model_specs(self, Models):
        """
        Load the model specs into the Track.specs properties

        Parameters
        ----------
        Models : Models class
            Specifications for all beacon models

        Returns
        -------
        None.

        """
        if not self.model:
            self.log.error("Model not known. Check input")

        # retrieve the default specs and the one for the beacon model in question
        self.specs.load_model_specs(self.model, Models)

    def load_metadata(self, Meta):
        """
        Read the metadata record for this track and overwrite track properties.

        Parameters
        ----------
        Meta : Meta object
            An instance of the class Meta representing a dataframe of metadata.

        """
        self.log.info("Reading track metadata from file")
        # filter records to find the data for this beacon
        record = Meta.df.loc[Meta.df.beacon_id == self.beacon_id]

        # check that one and only one record is returned
        if len(record) == 0:
            self.log.error("metadata not found, exiting....")
            raise Exception(f"The metadata for beacon {self.beacon_id} was not found")

        if len(record) > 1:
            self.log.error("beacon metadata duplicated, exiting....")
            raise Exception(
                f"The metadata search for beacon {self.beacon_id} returned duplicate records"
            )

        # load properties
        self.reader = record.reader.iloc[0]
        self.model = record.beacon_model.iloc[0]
        self.track_start = record.track_start.iloc[0]
        self.track_end = record.track_end.iloc[0]

        self.log.info(f"Beacon model: {self.model}")
        self.log.info(f"Track start: {self.track_start} and end: {self.track_end}")

        # convert to datetime and check for errors
        if self.track_start:
            try:
                self.track_start = pd.to_datetime(self.track_start, utc=True)
            except:
                self.log.error(
                    "Unrecognized track start format - trimming will not work as expected"
                )

        if self.track_end:
            try:
                self.track_end = pd.to_datetime(self.track_end, utc=True)
            except:
                self.log.error(
                    "Unrecognized track end format - trimming will not work as expected"
                )

    def clean(self):
        """
        Assign NaN to sensor values that exceed the minimum/maximum ranges.

        """
        self.log.info("Cleaning track")

        if not self.specs.make:
            self.log.error("No beacon specs are available, no cleaning attempted")
            return

        # Latitude
        self.data.loc[
            (self.data["latitude"] >= self.specs.latitude_max)
            | (self.data["latitude"] <= self.specs.latitude_min),
            "latitude",
        ] = np.nan

        # Longitude
        self.data.loc[
            (self.data["longitude"] >= self.specs.longitude_max)
            | (self.data["longitude"] <= self.specs.longitude_min)
            | (self.data["longitude"] == 0),
            "longitude",
        ] = np.nan

        # Air temperature
        self.data.loc[
            (self.data["temperature_air"] >= self.specs.temperature_air_max)
            | (self.data["temperature_air"] <= self.specs.temperature_air_min),
            "temperature_air",
        ] = np.nan

        # Internal temperature
        self.data.loc[
            (self.data["temperature_internal"] >= self.specs.temperature_internal_max)
            | (
                self.data["temperature_internal"] <= self.specs.temperature_internal_min
            ),
            "temperature_internal",
        ] = np.nan

        # Surface temperature
        self.data.loc[
            (self.data["temperature_surface"] >= self.specs.temperature_surface_max)
            | (self.data["temperature_surface"] <= self.specs.temperature_surface_min),
            "temperature_surface",
        ] = np.nan

        # Pressure
        self.data.loc[
            (self.data["pressure"] >= self.specs.pressure_max)
            | (self.data["pressure"] <= self.specs.pressure_min),
            "pressure",
        ] = np.nan

        # Pitch
        self.data.loc[
            (self.data["pitch"] >= self.specs.pitch_max)
            | (self.data["pitch"] <= self.specs.pitch_min),
            "pitch",
        ] = np.nan

        # Roll
        self.data.loc[
            (self.data["roll"] >= self.specs.roll_max)
            | (self.data["roll"] <= self.specs.roll_min),
            "roll",
        ] = np.nan

        # Heading
        self.data.loc[
            (self.data["heading"] >= self.specs.heading_max)
            | (self.data["heading"] <= self.specs.heading_min),
            "heading",
        ] = np.nan

        # Satellites
        self.data.loc[
            (self.data["satellites"] >= self.specs.satellites_max)
            | (self.data["satellites"] <= self.specs.satellites_min),
            "satellites",
        ] = np.nan

        # Battery voltage
        self.data.loc[
            (self.data["voltage"] >= self.specs.voltage_max)
            | (self.data["voltage"] <= self.specs.voltage_min),
            "voltage",
        ] = np.nan

        # Drop all rows where datetime_data, latitude or longitude is nan
        self.data.dropna(
            subset=["datetime_data", "latitude", "longitude"], inplace=True
        )

        self.data = self.data.round(
            {
                "temperature_air": 2,
                "temperature_internal": 2,
                "temperature_surface": 2,
                "pressure": 2,
                "pitch": 2,
                "roll": 2,
                "heading": 2,
                "voltage": 2,
            }
        )

        # reset the index
        self.data.reset_index(drop=True, inplace=True)

        # recalculate stats here since things may have changed
        self.stats()

        self.cleaned = True
        self.log.info("Track cleaned")

    def sort(self):
        """
        Order the track chronologically and remove redundant entries.

        """
        # sort by datetime_data, and loc_accuracy if available. The best loc_accuracy is the highest number
        self.data.sort_values(["datetime_data", "loc_accuracy"], inplace=True)
        # look for repeated values
        # sdf_dup = self.data.loc[self.data.duplicated(subset=["datetime_data"], keep=False)] # all lines
        sdf_dup = self.data.loc[
            self.data.duplicated(subset=["datetime_data"], keep="last")
        ]  # keep last dup
        self.log.info(f"There are {len(sdf_dup)} duplicates in this track")

        # remove all rows with duplicate times, prefer the one with best location accuracy
        self.data.drop_duplicates(
            subset=["datetime_data"], keep="last", inplace=True, ignore_index=True
        )

        # this should be true (check!)
        assert self.data[
            "datetime_data"
        ].is_monotonic_increasing, "Issue with timestamps, sort data!"

        # reset the index
        self.data.reset_index(drop=True, inplace=True)

        # recalculate stats here since things may have changed
        self.stats()

        self.sorted = True
        self.log.info(
            f"Track sorted, duplicates removed (if any). The track now has {self.observations} rows"
        )

    def speed(self):
        """
        Calculate speed, direction, distance and back azimuth between iceberg positions.

        Parameters
        ----------
        sdf : Pandas dataframe
            Standardized track data.  Note: clean and order it first!

        Returns
        -------
        sdf : Pandas dataframe
            Standardized track data - now with speed, etc.

        """
        # Ensure rows are sorted by datetime.
        assert self.data[
            "datetime_data"
        ].is_monotonic_increasing, "Issue with timestamps, sort data!"

        # Initialize pyproj with appropriate ellipsoid
        geodesic = pyproj.Geod(ellps="WGS84")

        # Calculate forward azimuth and great circle distance between modelled coordinates
        self.data["direction"], back_azimuth, self.data["distance"] = geodesic.inv(
            self.data["longitude"].shift().tolist(),
            self.data["latitude"].shift().tolist(),
            self.data["longitude"].tolist(),
            self.data["latitude"].tolist(),
        )

        # Convert azimuth from (-180° to 180°) to (0° to 360°)
        self.data["direction"] = ((self.data["direction"] + 360) % 360).round(2)

        # Calculate time delta between rows (in seconds)
        time_delta = self.data["datetime_data"].diff().dt.total_seconds()

        # Calculate speed in m/s
        self.data["speed"] = self.data["distance"] / time_delta

        # Round columns
        # df = df.round({"distance": 1, "speed": 2, "direction": 2}) # Not working?
        self.data["distance"] = self.data["distance"].round(0)
        self.data["direction"] = self.data["direction"].round(0)
        self.data["speed"] = self.data["speed"].round(3)

        # set property
        self.speeded = True
        self.log.info("Calculated displacement, direction and speed for track")

        # recalculate stats here since things may have changed
        self.stats()

    def speed_limit(self, threshold=10):
        """
        Remove gross speeding violations from data.

        Note the intent here is to remove only the very worst rows from datasets.  It is
        a very crude way to cut down on clearly wrong position data.  Note high speeds are
        due to inaccurate positions, but also inprecise positions over short periods of time.
        It is important to be careful.

        Parameters
        ----------
        sdf : Pandas dataframe
            Standardized track data.
        threshold : float, optional
            A threshold, beyond which rows are removed (m/s). The default is 10.

        Returns
        -------
        sdf : Pandas dataframe
            Standardized track data - now limited.

        """
        # needs to be in a loop since if there is a fly-away point, you have going out and coming back
        before = len(self.data)
        while (self.data["speed"] > threshold).any():
            self.data.drop(
                self.data[self.data["speed"] > threshold].index[0], inplace=True
            )
            self.speed()
        self.log.info(
            f"Removed {before - len(self.data)} rows due to speed limit violations"
        )

        # reset the index
        self.data.reset_index(drop=True, inplace=True)

        # recalculate stats here since things may have changed
        self.stats()

        self.speedlimited = True

    def trim(self):
        """
        Trim a track to a specified start and end time.

        Parameters
        ----------
        sdf : Pandas dataframe
            Standardized track data.
        track_start : datetime, optional
            track start time (earlier values will be removed). The default is None (no trim).
        track_end : datetime, optional
            track end time (later values will be removed). The default is None (no trim).

        Returns
        -------
        sdf : Pandas dataframe
            Standardized track data - now trimmed.

        """
        if self.track_start:
            self.data.drop(
                self.data[self.data["datetime_data"] < self.track_start].index,
                inplace=True,
            )
            self.log.info(
                f"Track start trimmed from {self.data_start} to {self.track_start}"
            )
            self.trimmed_start = True

        if self.track_end:
            self.data.drop(
                self.data[self.data["datetime_data"] > self.track_end].index,
                inplace=True,
            )
            self.log.info(f"Track end trimmed from {self.data_end} to {self.track_end}")
            self.trimmed_end = True

        # reset the index
        self.data.reset_index(drop=True, inplace=True)

        # recalculate stats here since things may have changed
        self.stats()

    def geo(self):
        """
        Add a geodataframe of track points and a track line to the track object.

        Returns
        -------
        None.

        """
        # Convert to GeoPandas dataframe
        self.trackpoints = gpd.GeoDataFrame(
            self.data,
            geometry=gpd.points_from_xy(self.data["longitude"], self.data["latitude"]),
        )

        # Set CRS
        self.trackpoints.crs = "EPSG:4326"

        # Convert to line
        self.trackline = self.trackpoints.groupby(["beacon_id"])["geometry"].apply(
            lambda x: LineString(x.tolist())
        )

        # Set CRS
        self.trackline.crs = "EPSG:4326"

        self.geoed = True
        self.log.info("Track geospatial data created")

    def output(self, types=["csv"], path_output=".", file_output=None):
        """
        Output the track to a file.

        Note the default is a csv (non-spatial) format.  Other options include track points
        (_pt) or track lines (_ln) in a kml or gpkg file [let's move on from shapefiles, eh!].
        See types option below.   Note that there is no fancy styling of the data.

        The script checks for an existing file.  If one is there, that will be logged.
        The file will not be overwritten.

        Parameters
        ----------
        types : list of output types to generate ['csv', 'pt_kml', 'ln_kml', 'pt_gpkg','ln_gpkg']. The default is 'csv'.
        path_output : str, optional
            Path to put the output. The default is the current directory
        file_output : str, optional
            filename of output. The default is None, which will autogenerate on the Beacon ID
        Returns
        -------
        None.

        """
        if not file_output:
            file_output = self.beacon_id

        # test if the geo method was run or not.
        if not self.geoed:
            self.geo()

        # output part
        if "csv" in types:
            # Write CSV file without index column
            if not os.path.isfile(f"{os.path.join(path_output, file_output)}.csv"):
                self.data.to_csv(
                    f"{os.path.join(path_output, file_output)}.csv", index=False
                )
                self.log.info("Track output as csv file")
            else:
                self.log.error("File already exists, writing as csv failed!")

        if "pt_gpkg" in types:
            if not os.path.isfile(f"{os.path.join(path_output, file_output)}_pt.gpkg"):
                self.trackpoints.to_file(
                    f"{os.path.join(path_output, file_output)}_pt.gpkg", driver="GPKG"
                )
                self.log.info("Track output as trackpoint gpkg file")
            else:
                self.log.error(
                    "File already exists, writing as trackpoint gpkg failed!"
                )

        if "ln_gpkg" in types:
            if not os.path.isfile(f"{os.path.join(path_output, file_output)}_ln.gpkg"):
                self.trackline.to_file(
                    f"{os.path.join(path_output, file_output)}_ln.gpkg", driver="GPKG"
                )
                self.log.info("Track output as trackline gpkg file")
            else:
                self.log.error("File already exists, writing as trackline gpkg failed!")

        if "pt_kml" in types:
            if not os.path.isfile(f"{os.path.join(path_output, file_output)}_pt.kml"):
                self.trackpoints.to_file(
                    f"{os.path.join(path_output, file_output)}_pt.kml", driver="KML"
                )
                self.log.info("Track output as trackpoint kml file")
            else:
                self.log.error("File already exists, writing as trackpoint kml failed!")

        if "ln_kml" in types:
            if not os.path.isfile(f"{os.path.join(path_output, file_output)}_ln.kml"):
                self.trackline.to_file(
                    f"{os.path.join(path_output, file_output)}_ln.kml", driver="KML"
                )
                self.log.info("Track output as trackline kml file")
            else:
                self.log.error("File already exists, writing as trackline kml failed!")

    def resample(self, timestep="D", agg_function=None, first=True):
        """
        Resample track to a given time step.

        Timestep can be D for daily or h for hourly or multiples of D or h (eg '7D', '12h')
        After resampling other track properties will be refreshed.
        Other agg_fuctions might be wanted (max?, min?) but these are not implemented. 
        One day maybe interpolations? 
        
        Since the track data and properies will be overwritten, it is a good idea to make a copy first: 
            test_track = track
            test_track.resample(timestep="6h")
           
        Note this has not been thoroughly tested! 
            
        Parameters
        ----------
        timestep : str, optional
            Give the code for the timestep to sample to. The default is "D". See above.
        agg_function : str, optional
            Aggregation function: median, mean, None. The default is None.
        first : bool, optional
            If agg_fuction is none, or for columns that cannot be aggreaged, \
                take first (True) or last (False) value for the time period. The default is True.
        
        Returns
        -------
        None.

        """
        sdf = self.data
        # need to have a datetime index or use the 'on' keyword
        sdf = sdf.set_index("datetime_data")
        sdf["u"] = np.sin(np.radians(sdf.heading))
        sdf["v"] = np.cos(np.radians(sdf.heading))

        # https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.resample.html
        # Note that you can control whether bin intervals are closed on left or right
        # and which label to use.  e.g., daily, closed left with left label:
        #   All data from 2024-08-02 00:00:00 to 2024-08-02 23:59:59 are in August 2.
        # Weekly and Montly have different behaviour by default so use use '7D' instead
        # of 'W' for weekly resampling for consistency
        # https://pandas.pydata.org/pandas-docs/stable/reference/groupby.html#api-groupby

        if agg_function == None:
            if first:
                number_f = "first"
            else:
                number_f = "last"
        elif agg_function == "mean":
            number_f = "mean"
        elif agg_function == "median":
            number_f = "median"
        else:
            self.log.error("Check resampling parameters")
            raise Exception("Check resampling parameters")
        if first:
            string_f = "first"
        else:
            string_f = "last"

        # this sort of thing is trivial
        sdf.resample("D").last()
        sdf.resample("D").first()

        # with mixed data types the way you aggregate needs to be controlled for each column
        sdf_ = sdf.resample(timestep).agg(
            beacon_id=("beacon_id", string_f),
            datetime_transmit=("datetime_transmit", number_f),
            latitude=("latitude", number_f),
            longitude=("longitude", number_f),
            temperature_air=("temperature_air", number_f),
            temperature_internal=("temperature_internal", number_f),
            temperature_surface=("temperature_surface", number_f),
            pressure=("pressure", number_f),
            pitch=("pitch", number_f),
            roll=("roll", number_f),
            heading=("heading", string_f),
            satellites=("satellites", number_f),
            voltage=("voltage", number_f),
            loc_accuracy=("loc_accuracy", number_f),
            u=("u", number_f),
            v=("v", number_f),
        )

        sdf_["heading"] = (360 + np.rad2deg(np.atan2(sdf_.u, sdf_.v))) % 360

        self.data = sdf_.drop(["u", "v"], axis=1)

        # after doing the resampling it will be important to run:
        self.refresh()

    def refresh(self):
        """
        Refresh all the stats and speed calcs, etc.

        Do this after you change anything in the dataframe
        track.data = track.data[track.data.loc_accuracy < 2]

        Returns
        -------
        None.
        """
        # reset the index, which copies the
        self.data.reset_index(inplace=True)

        # recalculate the speed and displacement
        self.speed()
        # self.speed_limit() # Assuming this is not needed here but leaving comment to flag this step
        # if you have geospatial data, it must be recreated
        if self.geoed:
            self.geo()

        self.stats()

    def track_metadata(self, meta_format=None, export=False):
        """
        Make a dictionary of the known track metadata for export.

        Parameters
        ----------
        meta_format : str, optional
            Specify 'pandas' or 'json' format. The default (None) is a dictionary.
        export : bool, optional
            Export to file in working directory. The default is False.

        Returns
        -------
        track_meta : TYPE
            DESCRIPTION.

        """
        # some tracks may not have beacon specs, let's find out...
        have_specs = False
        # get the specs for the beacon here but only run if you have specs
        # test to see if specs exist
        if "make" in self.specs.__dict__:
            have_specs = True
            s_meta = copy.deepcopy(self.specs.__dict__)

            # don't want every entry
            remove_keys = ["log", "deployment"]
            for key in remove_keys:
                s_meta.pop(key, None)  # Use pop to avoid KeyError if key doesn't exist

        # get all the properties of the track
        t_meta = copy.deepcopy(self.__dict__)

        # don't want every entry
        remove_keys = [
            "log",
            "data",
            "specs",
            "trackpoints",
            "trackline",
        ]
        for key in remove_keys:
            t_meta.pop(key, None)  # Use pop to avoid KeyError if key doesn't exist

        if have_specs:
            track_meta_dict = t_meta | s_meta
        else:
            track_meta_dict = t_meta

        if meta_format == "json":
            track_meta = json.dumps(track_meta_dict, indent=4)
            if export:
                with open(f"{self.beacon_id}_meta.json", "w") as file_export:
                    file_export.write(track_meta)
        if meta_format == "pandas":
            track_meta = pd.DataFrame([track_meta_dict])
            if export:
                track_meta.to_csv(f"{self.beacon_id}_meta.csv", index=False)

        return track_meta

    # The following graphing functions are in track_fig.py but listed here so they can
    # be a method of Track.

    def plot_map(self, path_output=".", interactive=False, dpi=300):
        """
        Plot a map of the track.

        See track_fig.py
        TODO:  configure to add *other_tracks to the plot

        Parameters
        ----------
        track : track object
            Standardized beacon track object.
        path_output : str, optional
            Path to save output. The default is ".".
        dpi : int, optional
            Resolution of the graph in dots per inch. The default is 300.
        interactive:
        log : logger object
            Enter a logger object

        Returns
        -------
        None.

        """

        # call the function in track_fig.py
        plot_map(
            self,
            path_output=path_output,
            dpi=dpi,
            interactive=interactive,
            log=self.log,
        )

    def plot_temp(self, path_output=".", interactive=False, dpi=300):
        """
        Plot a temperatures for the track.

        See track_fig.py
        TODO:  configure to add *other_tracks to the plot

        Parameters
        ----------
        track : track object
            Standardized beacon track object.
        path_output : str, optional
            Path to save output. The default is ".".
        dpi : int, optional
            Resolution of the graph in dots per inch. The default is 300.
        log : logger object
            Enter a logger object

        Returns
        -------
        None.

        """
        # call the function in track_fig.py
        plot_temp(
            self,
            path_output=path_output,
            dpi=dpi,
            interactive=interactive,
            log=self.log,
        )

    def plot_dist(self, path_output=".", interactive=False, dpi=300):
        """
        Plot distributions along the track.

        See track_fig.py
        TODO:  configure to add *other_tracks to the plot

        Parameters
        ----------
        track : track object
            Standardized beacon track object.
        path_output : str, optional
            Path to save output. The default is ".".
        dpi : int, optional
            Resolution of the graph in dots per inch. The default is 300.
        log : logger object
            Enter a logger object

        Returns
        -------
        None.

        """
        # call the function in track_fig.py
        plot_dist(
            self,
            path_output=path_output,
            dpi=dpi,
            interactive=interactive,
            log=self.log,
        )

    def plot_time(self, path_output=".", interactive=False, dpi=300):
        """
        Plot a timeseries of the track.

        See track_fig.py
        TODO:  configure to add *other_tracks to the plot

        Parameters
        ----------
        track : track object
            Standardized beacon track object.
        path_output : str, optional
            Path to save output. The default is ".".
        dpi : int, optional
            Resolution of the graph in dots per inch. The default is 300.
        log : logger object
            Enter a logger object

        Returns
        -------
        None.

        """
        # call the function in track_fig.py
        plot_time(
            self,
            path_output=path_output,
            dpi=dpi,
            interactive=interactive,
            log=self.log,
        )
