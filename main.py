import os
import time
from datetime import date, datetime
from io import BytesIO
from typing import Any

import boto3
import pandas as pd
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

import t212
from custom_utils import dataframe_utils, datetime_utils, decorators, email_utils


def get_input_dt() -> str:
    current_dt = date.today()
    previous_month_dt = current_dt - relativedelta(months=1)
    previous_month_dt_str = previous_month_dt.strftime('%Y-%m')

    print('Reporting Year Month in "YYYY-mm" format:')
    print(f'Or confirm default "{previous_month_dt_str}" by ENTER.')
    input_dt_str = input()

    if not input_dt_str:
        input_dt_str = previous_month_dt_str

    return input_dt_str


def transform(report_df: pd.DataFrame) -> pd.DataFrame:
    # Filter only buys and sells
    allowed_actions: list[str] = ['Market buy', 'Market sell']
    report_df = report_df[report_df['Action'].isin(allowed_actions)]

    # Filter out blacklisted tickers
    ticker_blacklist: list[str] = [
        'VNTRF',  # due to stock split
        'BRK.A',  # not available in digrin
    ]
    report_df = report_df[~report_df['Ticker'].isin(ticker_blacklist)]

    # Apply the mapping to the ticker column
    ticker_map: dict[str, str] = {
        'VWCE': 'VWCE.DE',
        'VUAA': 'VUAA.DE',
        'SXRV': 'SXRV.DE',
        'ZPRV': 'ZPRV.DE',
        'ZPRX': 'ZPRX.DE',
        'MC': 'MC.PA',
        'ASML': 'ASML.AS',
        'CSPX': 'CSPX.L',
        'EISU': 'EISU.L',
        'IITU': 'IITU.L',
        'IUHC': 'IUHC.L',
        'NDIA': 'NDIA.L',
        'NUKL': 'NUKL.DE',
        'AVWS': 'AVWS.DE',
    }
    report_df['Ticker'] = report_df['Ticker'].replace(ticker_map)

    # convert dtypes
    return report_df.convert_dtypes()


def main() -> None:
    load_dotenv(override=True)

    bucket_name: str = os.environ['BUCKET_NAME']
    t212_client = t212.APIClient(key=os.environ['T212_API_KEY'])
    seznam_client = email_utils.TLSClient(
        username=os.environ['EMAIL'],
        password=os.environ['EMAIL_PASSWORD'],
        host='smtp.seznam.cz',
    )
    s3_client = boto3.client('s3')

    input_dt_str: str = get_input_dt()  # used later in the naming of csv
    input_dt: datetime = datetime.strptime(input_dt_str, '%Y-%m')

    from_dt: datetime = datetime_utils.get_first_day_of_month(input_dt)
    to_dt: datetime = datetime_utils.get_first_day_of_next_month(input_dt)

    while True:
        report_id: int = t212_client.create_report(from_dt, to_dt)

        if report_id:
            break

        # limit 1 call per 30s
        time.sleep(10)

    # optimized wait time for report creation
    time.sleep(10)

    while True:
        # reports: list of dicts with keys:
        #   reportId, timeFrom, timeTo, dataIncluded, status, downloadLink
        reports: list[dict[str, Any]] = t212_client.list_reports()

        # too many calls -> fetch_reports returns None
        if not reports:
            # limit 1 call per 1min
            time.sleep(10)
            continue

        # filter report by report_id, start from the last report
        report_dict: dict[str, Any] = next(
            filter(lambda report: report.get('reportId') == report_id, reports[::-1])
        )

        if report_dict.get('status') == 'Finished':
            report = t212.Report(**report_dict)
            break

        decorators.logger.info('Report not yet ready.')
        time.sleep(10)

    t212_df_encoded: bytes = report.download()
    filename: str = f'{input_dt_str}.csv'
    s3_client.upload_fileobj(
        Fileobj=BytesIO(t212_df_encoded), Bucket=bucket_name, Key=f't212/{filename}'
    )

    t212_df: pd.DataFrame = dataframe_utils.decode_to_df(t212_df_encoded)
    digrin_df: pd.DataFrame = transform(t212_df)

    digrin_df_encoded: bytes = dataframe_utils.encode_df(digrin_df)
    s3_client.upload_fileobj(
        Fileobj=BytesIO(digrin_df_encoded), Bucket=bucket_name, Key=f'digrin/{filename}'
    )
    digrin_csv_url: str = s3_client.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket_name, 'Key': f'digrin/{filename}'},
        ExpiresIn=300,  # 5min
    )
    seznam_client.send_email(
        receiver=os.environ['EMAIL'],
        subject='T212 to Digrin',
        body=f'''
            <html>
                <body>
                    <p>
                        <br>Your Report is <a href="{digrin_csv_url}">ready</a>.<br>
                    </p>
                </body>
            </html>
        ''',
    )


if __name__ == '__main__':
    main()
