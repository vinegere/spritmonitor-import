#!/usr/bin/env python3
"""
Car Cost History Converter
==========================

Converts car usage cost history (fuelings, repairs, MOT, etc.)
between different online database formats.

Currently supported:
  Input:  Motostat.pl CSV export
  Output: Spritmonitor.de CSV import format (fuelings + costs)

Designed for extensibility with new input sources, output targets,
and transport mechanisms (file, API).

Requires Python 3.10+
"""

import csv
import logging
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Configuration & Constants
# ---------------------------------------------------------------------------

DEFAULT_INPUT_ENCODING = "utf-8"
DEFAULT_OUTPUT_ENCODING = "utf-8"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical Data Model
# ---------------------------------------------------------------------------
# All readers produce instances of CostEntry.
# All writers consume instances of CostEntry.
# This decouples input format knowledge from output format knowledge.

CostTypeValue = Literal["fueling", "cost"]
FuelingTypeValue = Literal["full", "partial", "first", "invalid"]
TiresTypeValue = Literal["summer", "winter", "all_season"]
DrivingStyleValue = Literal["eco", "normal", "dynamic"]

# Normalised fuel types based on Motostat strings and Spritmonitor API
FuelTypeValue = Literal[
    "diesel",
    "premium_diesel",
    "biodiesel",
    "gtl_diesel",
    "hvo100",
    "vegetable_oil",
    "petrol_95",
    "petrol_98",
    "petrol_super",
    "petrol_super_plus",
    "petrol_100",
    "petrol_100_plus",
    "petrol_normal",
    "e10",
    "two_stroke",
    "bioethanol",
    "lpg",
    "cng",
    "cng_h",
    "cng_l",
    "electricity",
    "green_electricity",
    "adblue",
    "hydrogen"
]

@dataclass
class CostEntry:
    """
    Canonical representation of a single cost history entry.

    This is the intermediate format that bridges all readers and writers.
    Fields are a superset of what various sources/targets may need.
    Optional fields are None when not applicable or not available.

    Design principles:
    - Store data in human-readable, semantic form (not encoded integers)
    - Encoding/decoding of service-specific formats is the responsibility
      of the respective Reader/Writer classes
    - Use the richest representation available (e.g. road type percentages
      rather than bitmask indicating if a road type was included
      in the route, since percentages can be reduced to bitmask
      but not vice versa)
    - Use Literal types where a fixed set of values is expected, for
      static analysis safety without the ceremony of Enum classes
    """

    # --- Core fields (applicable to all entry types) ---
    entry_date: date
    cost_type: CostTypeValue                    # "fueling" or "cost" - determines output routing
    odometer_km: int | None = None              # total mileage at the time of entry (km)
    trip_km: int | None = None                  # distance since last entry (km)
    cost_total: float | None = None             # total cost (fuel cost for fuelings,
                                                #   repair/service cost for non-fueling entries)
    cost_currency: str | None = None            # ISO 4217 currency code (PLN, EUR, USD, etc.)
    description: str | None = None              # Long text note

    # Fueling-specific fields
    fuel_quantity_liters: float | None = None   # amount of fuel filled (liters)
    fuel_price_per_liter: float | None = None   # price per liter (derived or from source)
    fuel_total_cost: float | None = None        # total fuel price (derived or from source)
    fuel_type: FuelTypeValue  | None = None     # canonical fuel type literal
    fueling_type: FuelingTypeValue | None = None # "full", "partial", "first", "invalid"

    # --- Driving context fields ---
    tires_type: TiresTypeValue | None = None  # "summer", "winter", "all_season"
    driving_style: DrivingStyleValue | None = None  # "eco", "normal", "dynamic"
    route_motorway_pct: int | None = None  # percentage of route on motorway (0-100)
    route_country_pct: int | None = None  # percentage of route on country roads (0-100)
    route_city_pct: int | None = None  # percentage of route in city (0-100)
    air_condition_pct: int | None = None  # percentage of A/C usage (0-100)

    # --- Board computer fields ---
    bc_consumption: float | None = None  # board computer consumption (L/100km)
    bc_quantity: float | None = None  # board computer fuel quantity (liters)
    bc_avg_speed: float | None = None  # board computer average speed (km/h)

    # --- Calculated / derived fields ---
    consumption: float | None = None  # calculated real consumption (L/100km)

    # --- Cost-specific fields (non-fueling) ---
    category: str | None = None  # detailed sub-category from source system
    #   (e.g., Motostat tags: "oil_and_filters",
    #   "inspection", "tire_service_and_tires",
    #   "electronics_and_electrics", "other")
    #   Writer maps this to target taxonomy.
    entry_name: str | None = None  # short human-readable label
    #   (e.g., "Olej", "Przeglad", "akumulator")

    # --- Location fields ---
    fuel_company: str | None = None  # fuel station brand/company name
    country: str | None = None  # country where entry occurred
    area: str | None = None  # region/area
    location: str | None = None  # specific location / address

    # --- Insurance-specific fields ---
    insurance_starts_on: date | None = None  # insurance coverage start date
    insurance_ends_on: date | None = None  # insurance coverage end date

    # --- Metadata ---
    source_raw: dict = field(default_factory=dict)  # preserve original row for debugging/audit


# ---------------------------------------------------------------------------
# Abstract Base Classes
# ---------------------------------------------------------------------------


class BaseReader(ABC):
    """
    Abstract base for all data readers.

    A reader is responsible for extracting data from a specific source
    (CSV file, API, database, etc.) and converting it into a list of
    canonical CostEntry objects.
    """

    @abstractmethod
    def read(self) -> list[CostEntry]:
        """
        Read from the source and return a list of CostEntry instances.

        Returns:
            List of canonical CostEntry objects.

        Raises:
            FileNotFoundError: If source file does not exist.
            ValueError: If source data cannot be parsed.
        """
        ...


class BaseWriter(ABC):
    """
    Abstract base for all data writers.

    A writer is responsible for taking a list of canonical CostEntry
    objects and writing them to a specific target (CSV file, API, etc.).
    """

    @abstractmethod
    def write(self, entries: list[CostEntry]) -> None:
        """
        Write the provided entries to the target.

        Args:
            entries: List of canonical CostEntry objects to output.

        Raises:
            IOError: If target cannot be written to.
            ValueError: If entries contain data incompatible with target format.
        """
        ...


# ---------------------------------------------------------------------------
# Motostat CSV Reader
# ---------------------------------------------------------------------------


class MotostatCsvReader(BaseReader):
    """
    Reader for CSV files exported from motostat.pl.

    Handles:
    - Parsing Motostat's specific CSV structure (column names, delimiters, encoding)
    - Mapping Motostat's cost categories to canonical CostType
    - Converting Motostat's date format to Python date objects
    - Handling Motostat's number formats (comma as decimal separator, etc.)
    """

    def __init__(self, file_path: Path, encoding: str = DEFAULT_INPUT_ENCODING):
        """
        Initialize the Motostat CSV reader.

        Args:
            file_path: Path to the Motostat CSV export file.
            encoding: File encoding (Motostat may export as UTF-8 or Windows-1250).
        """
        self.file_path = file_path
        self.encoding = encoding

    def read(self) -> list[CostEntry]:
        """
        Read and parse the Motostat CSV file.

        Returns:
            List of CostEntry objects, one per row in the CSV.
        """
        # TODO: Implement
        # Steps:
        # 1. Open and read CSV file
        # 2. Detect/validate header row against expected Motostat columns
        # 3. Iterate rows, calling _parse_row() for each
        # 4. Return collected entries
        raise NotImplementedError

    def _parse_row(self, row: dict) -> CostEntry:
        """
        Parse a single CSV row (as dict from DictReader) into a CostEntry.

        Args:
            row: Dictionary mapping column headers to cell values.

        Returns:
            A populated CostEntry instance.
        """
        # TODO: Implement
        # - Map Motostat columns to CostEntry fields
        # - Call helper methods for type detection, date parsing, number parsing
        raise NotImplementedError

    def _detect_cost_type(self, row: dict) -> CostType:
        """
        Determine the CostType from Motostat's category/type columns.

        Args:
            row: The raw CSV row dictionary.

        Returns:
            Appropriate CostType enum value.
        """
        # TODO: Implement mapping of Motostat categories to CostType
        raise NotImplementedError

    def _parse_date(self, date_string: str) -> date:
        """
        Parse Motostat's date format (e.g., 'DD.MM.YYYY' or 'YYYY-MM-DD') to date object.

        Args:
            date_string: Raw date string from CSV.

        Returns:
            Python date object.

        Raises:
            ValueError: If date string does not match expected formats.
        """
        # TODO: Implement with support for multiple date formats
        raise NotImplementedError

    def _parse_number(self, value: str) -> float | None:
        """
        Parse a numeric string from Motostat CSV, handling:
        - Comma as decimal separator
        - Optional thousands separator (space or dot)
        - Empty strings → None

        Args:
            value: Raw string value from CSV cell.

        Returns:
            Parsed float or None if empty/not applicable.
        """
        # TODO: Implement
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Spritmonitor CSV Writer
# ---------------------------------------------------------------------------


class SpritmonitorCsvWriter(BaseWriter):
    """
    Writer that produces CSV files compatible with Spritmonitor.de import.

    Handles:
    - Spritmonitor's expected column structure and naming
    - Spritmonitor's date format requirements
    - Spritmonitor's number format requirements (decimal separator, etc.)
    - Mapping canonical CostType back to Spritmonitor's type codes/names
    - Splitting entries into fueling records vs. cost records if needed
      (Spritmonitor may use separate import formats for these)
    """

    def __init__(self, file_path: Path, encoding: str = DEFAULT_OUTPUT_ENCODING):
        """
        Initialize the Spritmonitor CSV writer.

        Args:
            file_path: Path where the output CSV will be written.
            encoding: Output file encoding expected by Spritmonitor.
        """
        self.file_path = file_path
        self.encoding = encoding

    def write(self, entries: list[CostEntry]) -> None:
        """
        Write entries to a Spritmonitor-compatible CSV file.

        Args:
            entries: List of canonical CostEntry objects.
        """
        # TODO: Implement
        # Steps:
        # 1. Separate entries by type if Spritmonitor expects different formats
        # 2. Build header row(s) per Spritmonitor spec
        # 3. Convert each entry to output row via _format_row()
        # 4. Write CSV file
        raise NotImplementedError

    def _format_row(self, entry: CostEntry) -> dict:
        """
        Convert a single CostEntry to a dictionary matching Spritmonitor's
        expected CSV columns.

        Args:
            entry: Canonical CostEntry to format.

        Returns:
            Dictionary with Spritmonitor column names as keys.
        """
        # TODO: Implement
        raise NotImplementedError

    def _format_date(self, d: date) -> str:
        """
        Format a date to Spritmonitor's expected format (e.g., 'DD.MM.YYYY').

        Args:
            d: Python date object.

        Returns:
            Formatted date string.
        """
        # TODO: Implement
        raise NotImplementedError

    def _format_number(self, value: float | None, decimal_places: int = 2) -> str:
        """
        Format a number to Spritmonitor's expected format.

        Args:
            value: Numeric value or None.
            decimal_places: Number of decimal places.

        Returns:
            Formatted string, or empty string if value is None.
        """
        # TODO: Implement
        raise NotImplementedError

    def _map_fuel_type(self, fuel_type: str | None) -> str:
        """
        Map canonical fuel type string to Spritmonitor's fuel type code/name.

        Args:
            fuel_type: Canonical fuel type (e.g., "diesel", "petrol_95").

        Returns:
            Spritmonitor-compatible fuel type identifier.
        """
        # TODO: Implement
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_entries(entries: list[CostEntry]) -> list[str]:
    """
    Validate a list of CostEntry objects for data quality issues.

    Checks for:
    - Missing required fields (date is always required)
    - Logical inconsistencies (e.g., fuel quantity without fueling type)
    - Odometer going backwards (entries out of order or erroneous)
    - Suspicious values (negative costs, unrealistic fuel quantities)

    Args:
        entries: List of CostEntry objects to validate.

    Returns:
        List of warning/error messages. Empty list means all entries are valid.
    """
    # TODO: Implement
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Pipeline Orchestration
# ---------------------------------------------------------------------------


def convert(reader: BaseReader, writer: BaseWriter, skip_validation: bool = False) -> None:
    """
    Execute the full conversion pipeline: read → validate → write.

    This is the core orchestration function that ties together a reader
    and a writer with optional validation in between.

    Args:
        reader: An instance of a BaseReader subclass (data source).
        writer: An instance of a BaseWriter subclass (data target).
        skip_validation: If True, skip validation step (not recommended).

    Raises:
        SystemExit: If validation finds critical errors and user chooses to abort.
    """
    logger.info("Starting conversion pipeline")

    # Step 1: Read
    logger.info("Reading input data...")
    entries = reader.read()
    logger.info(f"Read {len(entries)} entries from source")

    # Step 2: Validate
    if not skip_validation:
        logger.info("Validating entries...")
        warnings = validate_entries(entries)
        if warnings:
            for w in warnings:
                logger.warning(w)
            logger.warning(f"Validation produced {len(warnings)} warning(s)")
        else:
            logger.info("Validation passed with no warnings")

    # Step 3: Write
    logger.info("Writing output data...")
    writer.write(entries)
    logger.info(f"Successfully wrote {len(entries)} entries to target")


# ---------------------------------------------------------------------------
# CLI Argument Parsing
# ---------------------------------------------------------------------------


def parse_arguments(args: list[str]) -> dict:
    """
    Parse command-line arguments.

    Expected arguments:
      - input_file: Path to input CSV (required)
      - output_file: Path to output CSV (required)
      - --input-format: Input format identifier (default: "motostat")
      - --output-format: Output format identifier (default: "spritmonitor")
      - --input-encoding: Override input file encoding
      - --output-encoding: Override output file encoding
      - --verbose / -v: Enable verbose/debug logging
      - --skip-validation: Skip the validation step

    Args:
        args: List of command-line argument strings (typically sys.argv[1:]).

    Returns:
        Dictionary of parsed arguments.
    """
    # TODO: Implement using argparse
    raise NotImplementedError


def build_reader(config: dict) -> BaseReader:
    """
    Factory function that instantiates the appropriate reader based on config.

    This is where new input formats are registered. Adding a new input source
    means adding a new elif branch here (or using a registry pattern).

    Args:
        config: Parsed CLI arguments / configuration dictionary.

    Returns:
        An instance of the appropriate BaseReader subclass.

    Raises:
        ValueError: If the requested input format is not supported.
    """
    # TODO: Implement
    # Currently supports: "motostat"
    # Future: "other_source", "api_source", etc.
    raise NotImplementedError


def build_writer(config: dict) -> BaseWriter:
    """
    Factory function that instantiates the appropriate writer based on config.

    This is where new output formats/targets are registered.

    Args:
        config: Parsed CLI arguments / configuration dictionary.

    Returns:
        An instance of the appropriate BaseWriter subclass.

    Raises:
        ValueError: If the requested output format is not supported.
    """
    # TODO: Implement
    # Currently supports: "spritmonitor"
    # Future: "spritmonitor_api", "other_format", etc.
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------


def setup_logging(verbose: bool = False) -> None:
    """
    Configure logging for the application.

    Args:
        verbose: If True, set log level to DEBUG; otherwise INFO.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------


def main() -> int:
    """
    Main entry point for the converter script.

    Orchestrates:
    1. Parse CLI arguments
    2. Set up logging
    3. Build reader and writer from configuration
    4. Execute conversion pipeline
    5. Handle errors gracefully and return appropriate exit code

    Returns:
        Exit code: 0 for success, non-zero for errors.
    """
    try:
        config = parse_arguments(sys.argv[1:])
        setup_logging(verbose=config.get("verbose", False))

        logger.info("Car Cost History Converter")
        logger.info(f"Input:  {config.get('input_file')} (format: {config.get('input_format')})")
        logger.info(f"Output: {config.get('output_file')} (format: {config.get('output_format')})")

        reader = build_reader(config)
        writer = build_writer(config)

        convert(reader, writer, skip_validation=config.get("skip_validation", False))

        logger.info("Conversion completed successfully.")
        return 0

    except FileNotFoundError as e:
        logger.error(f"File not found: {e}")
        return 1
    except ValueError as e:
        logger.error(f"Data error: {e}")
        return 2
    except KeyboardInterrupt:
        logger.info("Operation cancelled by user.")
        return 130
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return 99


if __name__ == "__main__":
    sys.exit(main())