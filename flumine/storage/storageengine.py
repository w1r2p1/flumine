import os
import datetime
import shutil
import zipfile
import logging
import boto3
from boto3.s3.transfer import (
    S3Transfer,
    TransferConfig,
)
from botocore.exceptions import BotoCoreError

logger = logging.getLogger(__name__)


class BaseEngine:

    NAME = None

    def __init__(self, directory, market_expiration=3600):
        self.directory = directory
        self.market_expiration = market_expiration
        self.markets_loaded = []
        self.validate_settings()

    def __call__(self, market_id, market_definition, stream_id):
        logger.info('Loading %s to %s:%s' % (market_id, self.NAME, self.directory))
        file_dir = os.path.join('/tmp', stream_id, market_id)

        # check that market has not already been loaded
        if market_id in self.markets_loaded:
            logger.warning('File: /tmp/%s/%s has already been loaded, updating..' % (stream_id, market_id))

        # check that file actually exists
        if not os.path.isfile(file_dir):
            logger.error('File: %s does not exist in /tmp/%s/' % (market_id, stream_id))
            return

        # zip file
        zip_file_dir = self.zip_file(file_dir, market_id, stream_id)

        # core load code
        self.load(zip_file_dir, market_definition)

        # clean up
        self.clean_up(stream_id)

        self.markets_loaded.append(market_id)

    def load(self, zip_file_dir, market_definition):
        """Loads zip_file_dir to expected dir
        """
        raise NotImplementedError

    def validate_settings(self):
        """Validates settings e.g. directory
        provided, raises OSError
        """
        pass

    @staticmethod
    def create_metadata(market_definition):
        try:
            del market_definition['runners']
        except KeyError:
            pass
        return dict([a, str(x)] for a, x in market_definition.items())

    def clean_up(self, stream_id):
        """If zip > 1hr old remove zip and txt
        file
        """
        directory = os.path.join('/tmp', stream_id)

        for file in os.listdir(directory):
            if file.endswith('.zip'):
                file_stats = os.stat(
                    os.path.join(directory, file)
                )
                last_modified = datetime.datetime.fromtimestamp(file_stats.st_mtime)
                seconds_since = (datetime.datetime.now()-last_modified).total_seconds()

                if seconds_since > self.market_expiration:
                    logging.info('Removing: %s, age: %ss' % (file, round(seconds_since, 2)))

                    txt_path = os.path.join(directory, file.split('.zip')[0])
                    zip_path = os.path.join(directory, file)

                    os.remove(txt_path)
                    os.remove(zip_path)

    @staticmethod
    def zip_file(file_dir, market_id, stream_id):
        """zips txt file into filename.zip
        """
        zip_file_directory = os.path.join('/tmp', stream_id, '%s.zip' % market_id)
        with zipfile.ZipFile(zip_file_directory, mode='w') as zf:
            zf.write(file_dir, os.path.basename(file_dir), compress_type=zipfile.ZIP_DEFLATED)
        return zip_file_directory

    @property
    def markets_loaded_count(self):
        return len(self.markets_loaded)

    @property
    def extra(self):
        return {
            'name': self.NAME,
            'markets_loaded': self.markets_loaded,
            'markets_loaded_count': self.markets_loaded_count,
            'directory': self.directory,
        }


class Local(BaseEngine):

    NAME = 'LOCAL'

    def validate_settings(self):
        if not os.path.isdir(self.directory):
            raise OSError('File dir %s does not exist' % self.directory)

    def load(self, zip_file_dir, market_definition):
        shutil.copy(zip_file_dir, self.directory)


class S3(BaseEngine):

    NAME = 'S3'

    def __init__(self, directory, access_key=None, secret_key=None):
        self.s3 = boto3.client(
            's3',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        transfer_config = TransferConfig(use_threads=False)
        self.transfer = S3Transfer(self.s3, config=transfer_config)
        super(S3, self).__init__(directory)

    def validate_settings(self):
        # error raised if bucket not present
        self.s3.head_bucket(Bucket=self.directory)

    def load(self, zip_file_dir, market_definition):
        event_type_id = market_definition.get('eventTypeId')
        try:
            self.transfer.upload_file(
                filename=zip_file_dir,
                bucket=self.directory,
                key=os.path.join(
                    'marketdata', 'streaming', event_type_id, os.path.basename(zip_file_dir)
                ),
                extra_args={
                    'Metadata': self.create_metadata(market_definition)
                }
            )
            logger.info('%s successfully loaded to s3' % zip_file_dir)
        except (BotoCoreError, Exception) as e:
            logger.error('Error loading to s3: %s' % e)
