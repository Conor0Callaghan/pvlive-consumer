""" Application for getting live GSP data

1. Load GSP ids from database
2. For each site, find the most recent data in a database
3. Pull data from https://www.solar.sheffield.ac.uk/pvlive/, if more data is available.
4. Save data to database - extra: check no duplicate data is added to the database
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import click
import pandas as pd
from nowcasting_datamodel.connection import DatabaseConnection
from nowcasting_datamodel.models.base import Base_Forecast
from nowcasting_datamodel.models.gsp import GSPYield, GSPYieldSQL, LocationSQL
from nowcasting_datamodel.read.read import update_latest_input_data_last_updated
from pvlive_api import PVLive
from sqlalchemy.orm import Session

import gspconsumer
from gspconsumer.gsps import filter_gsps_which_have_new_data, get_gsps

logging.basicConfig(
    level=getattr(logging, os.getenv("LOGLEVEL", "DEBUG")),
    format="[%(asctime)s] {%(pathname)s:%(lineno)d} %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@click.command()
@click.option(
    "--db-url",
    default=None,
    envvar="DB_URL",
    help="The Database URL where forecasts will be saved",
    type=click.STRING,
)
@click.option(
    "--regime",
    default="in-day",
    envvar="REGIME",
    help="regime of which to pull, either 'in-day' or 'day-after'",
    type=click.STRING,
)
@click.option(
    "--n-gsps",
    default=10,
    envvar="N_GSPS",
    help="Number of gsps data to pull",
    type=click.STRING,
)
@click.option(
    "--include-national",
    default=True,
    envvar="INCLUDE_NATIONAL",
    help="Get the national data as well",
    type=click.BOOL,
)
def app(db_url: str, regime: str = "in-day", n_gsps: int = 317, include_national: bool = True):
    """
    Run GSP consumer app, this collect GSP live data and save it to a database.

    :param db_url: the Database url to save the PV system data
    :param regime: if its "in-day" or "day-after"
    :param n_gsps: How many gsps of data to pull
    :param include_national: optionl if to get national data or not
    """

    logger.info(f"Running GSP Consumer app ({gspconsumer.__version__}) for regime {regime}")

    n_gsps = int(n_gsps)

    include_national = bool(include_national)
    total_n_gsps = n_gsps + 1 if include_national else n_gsps

    connection = DatabaseConnection(url=db_url, base=Base_Forecast, echo=True)
    with connection.get_session() as session:
        # 1. Read list of GSP systems (from local file)
        # and get their refresh times (refresh times can also be stored locally)
        logger.debug("Read list of GSP from database")
        gsps = get_gsps(
            session=session, n_gsps=n_gsps, regime=regime, include_national=include_national
        )
        assert (
            len(gsps) == total_n_gsps
        ), f"There are {len(gsps)} GSPS, there should be {total_n_gsps}"

        # 2. Find most recent entered data (for each GSP) in OCF database,
        # and filter depending on refresh rate
        logger.debug(
            "Find most recent entered data (for each GSP) in OCF database,"
            "and filter GSP depending on refresh rate"
        )
        gsps = filter_gsps_which_have_new_data(gsps=gsps)
        assert (
            len(gsps) <= total_n_gsps
        ), f"There are {len(gsps)} GSPS, there should be <= {total_n_gsps}"

        # 3. Pull data
        pull_data_and_save(gsps=gsps, session=session, regime=regime)


def pull_data_and_save(
    gsps: List[LocationSQL],
    session: Session,
    datetime_utc: Optional[None] = None,
    regime: str = "in-day",
):
    """
    Pull the gsp yield data and save to database

    :param gsps: list of gsps to save
    :param session: database sessions
    :param provider: provider name
    :param datetime_utc: datetime now, this is optional
    """

    pvlive = PVLive()
    pvlive.base_url = "https://api0.solar.sheffield.ac.uk/pvlive/api/v4/"

    if datetime_utc is None:
        datetime_utc = datetime.utcnow().replace(tzinfo=timezone.utc)  # add timezone

    if regime == "in-day":
        start = datetime_utc - timedelta(hours=1)
        end = datetime_utc + timedelta(minutes=30)
    else:
        start = datetime_utc.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            hours=24
        )
        end = datetime_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    logger.info(f"Pulling data for {len(gsps)} GSP for {datetime_utc}")

    all_gsps_yields_sql = []
    for gsp in gsps:

        gsp_yield_df: pd.DataFrame = pvlive.between(
            start=start, end=end, entity_type="gsp", entity_id=gsp.gsp_id, dataframe=True
        )

        logger.debug(f"Processing GSP ID {gsp.gsp_id} ({gsp.label}), out of {len(gsps)}")

        logger.debug(f"Got {len(gsp_yield_df)} gsp yield for gsp id {gsp.gsp_id} before filtering")

        if len(gsp_yield_df) == 0:
            logger.warning(f"Did not find any data for {gsp.gsp_id} for {start} to {end}")
        else:

            # filter by datetime
            gsp_yield_df = gsp_yield_df[gsp_yield_df["datetime_gmt"] >= start]
            gsp_yield_df = gsp_yield_df[gsp_yield_df["datetime_gmt"] < end]

            # filter by last
            if gsp.last_gsp_yield is not None:
                last_gsp_datetime = gsp.last_gsp_yield.datetime_utc.replace(tzinfo=timezone.utc)
                gsp_yield_df = gsp_yield_df[gsp_yield_df["datetime_gmt"] > last_gsp_datetime]

                if len(gsp_yield_df) == 0:
                    logger.debug(
                        f"No new data available after {last_gsp_datetime}. "
                        f"Last data point was {last_gsp_datetime}"
                    )
            else:
                logger.debug(f"This is the first lot gsp yield data for GSP {(gsp.gsp_id)}")

            # need columns datetime_utc, solar_generation_kw
            gsp_yield_df["solar_generation_kw"] = 1000 * gsp_yield_df["generation_mw"]
            gsp_yield_df["datetime_utc"] = gsp_yield_df["datetime_gmt"]
            gsp_yield_df = gsp_yield_df[["solar_generation_kw", "datetime_utc"]]
            gsp_yield_df["regime"] = regime

            # change to list of pydantic objects
            gsp_yields = [GSPYield(**row) for row in gsp_yield_df.to_dict(orient="records")]

            # change to sqlalamcy objects and add gsp systems
            gsp_yields_sql = [gsp_yield.to_orm() for gsp_yield in gsp_yields]
            for gsp_yield_sql in gsp_yields_sql:
                gsp_yield_sql.location = gsp

            logger.debug(f"Found {len(gsp_yields_sql)} gsp yield for GSPs {gsp.gsp_id}")

            all_gsps_yields_sql = all_gsps_yields_sql + gsp_yields_sql

            if len(all_gsps_yields_sql) > 100:
                # 4. Save to database - perhaps check no duplicate data. (for each GSP)
                save_to_database(session=session, gsp_yields=all_gsps_yields_sql)
                all_gsps_yields_sql = []

    # 4. Save to database - perhaps check no duplicate data. (for each GSP)
    save_to_database(session=session, gsp_yields=all_gsps_yields_sql)


def save_to_database(session: Session, gsp_yields: List[GSPYieldSQL]):
    """
    Save GSP yield data to database

    :param session: database session
    :param gsp_yields: list of gsp data
    """
    logger.debug(f"Will be adding {len(gsp_yields)} gsp yield object to database")

    session.add_all(gsp_yields)
    session.commit()

    update_latest_input_data_last_updated(session=session, component="gsp")


if __name__ == "__main__":
    app()
