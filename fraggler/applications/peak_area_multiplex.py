import logging
import re
import pandas as pd
import numpy as np
from scipy.signal import find_peaks, peak_widths
from lmfit.models import VoigtModel, GaussianModel, LorentzianModel
from fraggler.ladder_fitting.fit_ladder_model import FitLadderModel


######### -------------------- Custom Errors ######### --------------------
class OverlappingIntervalError(Exception):
    pass


class WrongColumnsError(Exception):
    pass


######### -------------------- Validation functions ######### --------------------
def is_overlapping(df: pd.DataFrame) -> bool:
    test = (
        df.sort_values("start")
        .assign(intervals=lambda x: [range(y.start, y.stop) for y in x.itertuples()])
        .explode("intervals")
    )

    if test.shape[0] != test.intervals.nunique():
        dups = (
            test.value_counts("intervals")
            .reset_index()
            .sort_values("intervals")
            .loc[lambda x: x[0] > 1]
            .iloc[0, 0]
        )
        logging.warning(f"""
        Customized peaks contains overlapping ranges
        Starting at value: {dups}
        """)
        return True
    return False


def has_columns(df: pd.DataFrame) -> bool:
    columns = set(["name", "start", "stop", "amount"])
    df_columns = set(df.columns)

    if len(columns) != len(df_columns):
        logging.warning(f"""
        Customized peaks table does not containg the right columns.
        Current columns: {df_columns}
        Needed columns: {columns}
        """)
        return False

    intersection = columns.intersection(df_columns)
    if len(intersection) != len(df_columns):
        logging.warning(f"""
        Customized peaks table does not containg the right columns.
        Current columns: {df_columns}
        Needed columns: {columns}
        """)
        return False
    
    return True


#### ---------------------------------------------------------------------------------- ###


class PeakAreaDeMultiplexIterator:
    def __init__(self, number_of_assays):
        self.number_of_assays = number_of_assays
        self.current = 0

    def __next__(self):
        if self.current >= self.number_of_assays:
            raise StopIteration
        else:
            result = self.current
            self.current += 1
            return result


# TOD# TODO
# Fit peak model with x == time instead of x == bp?
class PeakAreaDeMultiplex:
    """
    Class for finding peak areas and quotients of peaks in a given data set.

    Parameters:
    -----------
    model: FitLadderModel
        A FitLadderModel object containing the data set to be analyzed.
    peak_finding_model: str
        The name of the peak-finding model to be used.
    min_ratio: float, optional (default=0.2)
        The minimum ratio of peak height to highest peak height required to consider a peak as valid.
    search_peaks_start: int, optional (default=50)
        The starting point in basepairs for the search for peaks.

    Attributes:
    -----------
    model: FitLadderModel
        A FitLadderModel object containing the data set to be analyzed.
    raw_data: pd.DataFrame
        The raw data from the FitLadderModel object.
    file_name: str
        The name of the file associated with the FitLadderModel object.
    search_peaks_start: int
        The starting point in basepairs for the search for peaks.
    found_peaks: bool
        A flag indicating whether any peaks were found.
    peaks_index: np.ndarray
        An array of the indices of the peaks found.
    peaks_dataframe: pd.DataFrame
        A DataFrame of the peaks found, with basepairs and peak heights.
    peak_information: pd.DataFrame
        A DataFrame of the peaks found, with basepairs, peak heights, ratios, and peak names.
    peak_widths: pd.DataFrame
        A DataFrame of the peaks found, with basepairs, peak heights, start and end indices, and peak names.
    divided_peaks: List[pd.DataFrame]
        A list of DataFrames, each containing a single peak and its associated data.
    fit_df: List[pd.DataFrame]
        A list of DataFrames, each containing the raw data and the best-fit curve for a single peak.
    fit_params: List[dict]
        A list of dictionaries, each containing the parameters of the best-fit curve for a single peak.
    fit_report: List[str]
        A list of strings, each containing the report of the best-fit curve for a single peak.
    quotient: float
        The quotient of the areas of the peaks, calculated as the last peak divided by the mean of the peaks to the left of it.
    """

    def __init__(
        self,
        model: FitLadderModel,
        min_ratio: float = 0.15,
        search_peaks_start: int = 110,
        peak_height: int = 350,
        distance_between_assays: int = 15,
        cutoff: float = None,
        custom_peaks: str | pd.DataFrame = None,
    ) -> None:
        self.model = model
        self.raw_data = self.model.adjusted_baisepair_df
        self.file_name = self.model.fsa_file.file_name
        self.search_peaks_start = search_peaks_start
        self.cutoff = cutoff or None
        self.peak_height = peak_height
        self.min_ratio = min_ratio
        self.distance_between_assays = distance_between_assays
        self.custom_peaks = (
            custom_peaks.fillna(0)
            if isinstance(custom_peaks, pd.DataFrame)
            else pd.read_csv(custom_peaks).fillna(0)
            if isinstance(custom_peaks, str)
            else None
        )

        # Validation of custom_peaks
        self.run_validation()
        # find peaks, custom or agnostic
        self.find_peaks()
        # continue whether peaks are found or not.
        self.found_peaks()

    def __iter__(self):
        return PeakAreaDeMultiplexIterator(self.number_of_assays)

    def run_validation(self):
        if self.custom_peaks is None:
            return

        if is_overlapping(self.custom_peaks):
            raise OverlappingIntervalError("Overlapping intervals!")
            exit(1)

        if not has_columns(self.custom_peaks):
            raise WrongColumnsError("Wrong columns!")
            exit(1)

    def find_peaks(self):
        if self.custom_peaks is not None:
            self.find_peaks_customized(
                peak_height=self.peak_height,
            )
        else:
            self.find_peaks_agnostic(
                peak_height=self.peak_height,
                min_ratio=self.min_ratio,
                distance_between_assays=self.distance_between_assays,
            )

    def found_peaks(self):
        # if no peaks could be found
        if self.peak_information.shape[0] == 0:
            self.found_peaks = False
            logging.warning(f"No peaks could be found. Please look at raw data.")
        # if peaks are found
        else:
            self.found_peaks = True
            # find peak widths
            self.find_peak_widths()
            # divade peaks based on their assay they belonging
            self.divided_assays = self.divide_peak_assays()
            # how many assays does this sample contain?
            self.number_of_assays = len(self.divided_assays)
            # divide all peaks in each assay into separate dataframes
            self.divided_peaks = [self.divide_peaks(x) for x in self.divided_assays]
            # logging
            logging.info(f"""
            Number of assays found: {self.number_of_assays}
            Number of peaks found: {self.peak_information.shape[0]}
            """)

    def find_peaks_agnostic(
        self,
        peak_height: int,
        min_ratio: float,
        distance_between_assays: int,
    ) -> None:
        peaks_dataframe = self.raw_data.loc[
            lambda x: x.basepairs > self.search_peaks_start
        ]
        peaks_index, _ = find_peaks(peaks_dataframe.peaks, height=peak_height)

        peak_information = (
            peaks_dataframe.iloc[peaks_index]
            .assign(peaks_index=peaks_index)
            .assign(peak_name=lambda x: range(1, x.shape[0] + 1))
            # separate the peaks into different assay groups depending on the distance
            # between the peaks
            .assign(difference=lambda x: x.basepairs.diff())
            .fillna(100)
            .assign(
                assay=lambda x: np.select(
                    [x.difference > distance_between_assays],
                    [x.peak_name * 10],
                    default=pd.NA,
                )
            )
            .fillna(method="ffill")
            .assign(max_peak=lambda x: x.groupby("assay")["peaks"].transform(np.max))
            .assign(ratio=lambda x: x.peaks / x.max_peak)
            .loc[lambda x: x.ratio > min_ratio]
            .assign(peak_name=lambda x: range(1, x.shape[0] + 1))
        )

        # update peaks_index based on the above filtering
        peaks_index = peak_information.peaks_index.to_numpy()

        # update class attributes
        self.peaks_index = peaks_index
        self.peaks_dataframe = peaks_dataframe
        self.peak_information = peak_information

    def find_peaks_customized(
        self,
        peak_height: int,
    ) -> None:

        # Filter where to start search for the peaks
        peaks_dataframe = self.raw_data.loc[
            lambda x: x.basepairs > self.search_peaks_start
        ]
        # Find the peaks
        peaks_index, _ = find_peaks(peaks_dataframe.peaks, height=peak_height)

        # Filter the df to get right peaks
        peak_information = peaks_dataframe.iloc[peaks_index].assign(
            peaks_index=peaks_index
        )
        # Filter the above df based on the custom peaks from the user
        customized_peaks = []
        for assay in self.custom_peaks.itertuples():
            df = (
                peak_information.loc[lambda x: x.basepairs > assay.start]
                .loc[lambda x: x.basepairs < assay.stop]
                .assign(assay=assay.name)
            )

            # Rank the peaks by height and filter out the smallest ones
            if assay.amount != 0:
                df = (
                    df.assign(rank_peak=lambda x: x.peaks.rank(ascending=False))
                    .loc[lambda x: x.rank_peak <= assay.amount]
                    .drop(columns=["rank_peak"])
                )

            customized_peaks.append(df)

        peak_information = (
            pd.concat(customized_peaks)
            .reset_index()
            .assign(peak_name=lambda x: range(1, x.shape[0] + 1))
        )

        # update peaks_index based on the above filtering
        peaks_index = peak_information.peaks_index.to_numpy()

        # update class attributes
        self.peaks_index = peaks_index
        self.peaks_dataframe = peaks_dataframe
        self.peak_information = peak_information

    def find_peak_widths(self, rel_height: float = 0.95):
        widths = peak_widths(
            self.peaks_dataframe.peaks,
            self.peaks_index,
            rel_height=rel_height,
        )

        df = pd.DataFrame(widths).T
        df.columns = ["x", "peak_height", "peak_start", "peak_end"]

        self.peak_widths = (
            df.assign(peak_start=lambda x: np.floor(x.peak_start).astype(int))
            .assign(peak_end=lambda x: np.ceil(x.peak_end).astype(int))
            .assign(peak_name=lambda x: range(1, x.shape[0] + 1))
            .merge(self.peak_information, on="peak_name")
        )

    def divide_peak_assays(self) -> list[pd.DataFrame]:
        """
        Divide the peaks belonging to different assays based on their assay number
        """
        df = self.peak_widths
        return [df.loc[df.assay == x] for x in df.assay.unique()]

    def divide_peaks(self, assay: pd.DataFrame, padding: int = 4) -> list[pd.DataFrame]:
        # add some padding to the left and right to be sure to include everything in the peak
        return [
            self.peaks_dataframe.iloc[x.peak_start - padding : x.peak_end + padding]
            for x in assay.itertuples()
        ]

    def fit_lmfit_model(self, peak_finding_model: str, assay_number: int):
        if assay_number >= self.number_of_assays:
            raise IndexError(
                f"""
                The sample only contains {self.number_of_assays} assays. 
                Use a number inside of the range.
                Indexing starts at 0.
                """
            )

        if peak_finding_model == "gauss":
            model = GaussianModel()
        elif peak_finding_model == "voigt":
            model = VoigtModel()
        elif peak_finding_model == "lorentzian":
            model = LorentzianModel()
        else:
            raise NotImplementedError(
                f"""
                {peak_finding_model} is not implemented! 
                Options: [gauss, voigt, lorentzian]
                """
            )

        fit_df = []
        fit_params = []
        fit_report = []
        for df in self.divided_peaks[assay_number]:
            df = df.copy()
            y = df.peaks.to_numpy()
            # CHANGED to time instead of basepair
            x = df.time.to_numpy()

            params = model.guess(y, x)
            out = model.fit(y, params, x=x)

            fit_df.append(df.assign(fitted=out.best_fit, model=peak_finding_model))
            fit_params.append(out.values)
            fit_report.append(out.fit_report())

        # Update the instances of the model fit
        self.fit_df = fit_df
        self.fit_params = fit_params
        self.fit_report = fit_report

    # TODO
    # Fix so that the cutoff is a range or something else.
    def calculate_quotient(
        self,
    ) -> None:

        """
        :Params:
        """
        areas = np.array([x["amplitude"] for x in self.fit_params])

        right_by_left = True
        if self.cutoff is not None:
            if pd.concat(self.fit_df).basepairs.mean() < self.cutoff:
                right_by_left = False

        # if there only is 1 peak, return 0
        if len(areas) == 1:
            self.quotient = 0
            return

        # if there only are 2 peaks, return the quotient
        if len(areas) == 2:
            # left peak divided by right peak
            if not right_by_left:
                self.quotient = areas[0] / areas[1]
                return
            # right peak divided by left peak
            self.quotient = areas[1] / areas[0]
            return

        # TODO change this to the proper assay
        # return the last peak divided by the mean of the peaks to the left of it
        self.quotient = areas[-1] / areas[:-1].mean()

    def peak_position_area_dataframe(self, assay_number: int) -> pd.DataFrame:
        """
        Returns a DataFrame of each peak and its properties
        """
        dataframes = []
        for i, _ in enumerate(self.fit_df):
            report = self.fit_report[i]
            r_value = float(re.findall(r"R-squared *= (0\.\d{3})", report)[0])
            df = (
                self.fit_df[i]
                .loc[lambda x: x.peaks == x.peaks.max()]
                .assign(area=self.fit_params[i]["amplitude"])
                .assign(r_value=r_value)
                .assign(peak_name=f"Peak {i + 1}")
                .drop(columns="time")
                .reset_index(drop=True)
                .rename(
                    columns={
                        "peaks": "peak_height",
                        "fitted": "fitted_peak_height",
                    }
                )
                .drop_duplicates("peak_name")
                .assign(file_name=self.file_name)
            )
            dataframes.append(df)

        self.assay_peak_area_df = pd.concat(dataframes).assign(
            quotient=self.quotient,
            peak_number=lambda x: x.shape[0],
            assay_number=assay_number + 1,
        )

    def fit_assay_peaks(
        self,
        peak_finding_model: str,
        assay_number: int,
    ) -> None:
        """
        Runs fit_lmfit_model, calculate_quotient and peak_position_area_dataframe
        """
        self.fit_lmfit_model(peak_finding_model, assay_number)
        self.calculate_quotient()
        self.peak_position_area_dataframe(assay_number)
        return self.assay_peak_area_df

    def assays_dataframe(self, peak_finding_model: str = "gauss"):
        dfs = []
        for i in self:
            dfs.append(self.fit_assay_peaks(peak_finding_model, i))
        return pd.concat(dfs, ignore_index=True)