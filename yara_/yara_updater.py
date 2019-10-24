import glob
import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
from typing import List, Dict, Any, Optional, Set
from urllib.parse import urlparse
from zipfile import ZipFile
import requests
import yaml
import yara
from assemblyline_client import Client
from git import Repo

from assemblyline.common import log as al_log
from assemblyline.common.digests import get_sha256_for_file
from assemblyline.common.isotime import now_as_iso
from yara_.yara_importer import YaraImporter
from yara_.yara_validator import YaraValidator

al_log.init_logging('service_updater')

LOGGER = logging.getLogger('assemblyline.service_updater')


UPDATE_CONFIGURATION_PATH = os.environ.get('UPDATE_CONFIGURATION_PATH', None)
UPDATE_OUTPUT_PATH = os.environ.get('UPDATE_OUTPUT_PATH', None)
UPDATE_DIR = os.path.join(tempfile.gettempdir(), 'yara_updates')

YARA_EXTERNALS = {f'al_{x}': x for x in ['submitter', 'mime', 'tag']}


def _compile_rules(rules_file, source_name):
    """
    Saves Yara rule content to file, validates the content with Yara Validator, and uses Yara python to compile
    the rule set.

    Args:
        rules_txt: Yara rule file content.

    Returns:
        Compiled rules, compiled rules md5.
    """
    try:
        validate = YaraValidator(externals=YARA_EXTERNALS, logger=LOGGER)
        edited = validate.validate_rules(rules_file)
    except Exception as e:
        raise e
    # Grab the final output if Yara Validator found problem rules
    # if edited:
        # with open(rules_file, 'r') as f:
        #     sdata = f.read()
        # first_line, clean_data = sdata.split('\n', 1)
        # if first_line.startswith(prefix):
        #     last_update = first_line.replace(prefix, '')
        # else:
        #     last_update = now_as_iso()
        #     clean_data = sdata

    # Try to compile the final/cleaned yar file
    rules = yara.compile(rules_file, externals=YARA_EXTERNALS)

    return True


def url_download(source: Dict[str, Any], previous_update: Optional[float] = None) -> Optional[str]:
    """

    :param source:
    :param previous_update:
    :return:
    """
    name = source['name']
    uri = source['uri']
    username = source.get('username', None)
    password = source.get('password', None)
    auth = (username, password) if username and password else None

    headers = source.get('headers', None)

    # Create a requests session
    session = requests.Session()

    try:
        # Check the response header for the last modified date
        response = session.head(uri, auth=auth, headers=headers)
        last_modified = response.headers.get('Last-Modified', None)
        if last_modified:
            # Convert the last modified time to epoch
            last_modified = time.mktime(time.strptime(last_modified, "%a, %d %b %Y %H:%M:%S %Z"))

            # Compare the last modified time with the last updated time
            if previous_update and last_modified > previous_update:
                # File has not been modified since last update, do nothing
                return

        if previous_update:
            previous_update = time.strftime("%a, %d %b %Y %H:%M:%S %Z", time.gmtime(previous_update))
            if headers:
                headers['If-Modified-Since'] = previous_update
            else:
                headers = {'If-Modified-Since': previous_update}

        response = session.get(uri, auth=auth, headers=headers)

        # Check the response code
        if response.status_code == requests.codes['not_modified']:
            # File has not been modified since last update, do nothing
            return
        elif response.ok:
            file_name = os.path.basename(urlparse(uri).path) # TODO: make filename as source name with extension .yar
            file_path = os.path.join(UPDATE_DIR, file_name)
            with open(file_path, 'wb') as f:
                f.write(response.content)

            # Return the SHA256 of the downloaded file
            return get_sha256_for_file(file_path)
    except requests.Timeout:
        # TODO: should we retry?
        pass
    except Exception as e:
        # Catch all other types of exceptions such as ConnectionError, ProxyError, etc.
        LOGGER.info(str(e))
        exit()  # TODO: Should we exit even if one file fails to download? Or should we continue downloading other files?
    finally:
        # Close the requests session
        session.close()


def git_clone_repo(source: Dict[str, Any]) -> List[str] and List[str]:
    name = source['name']
    url = source['uri']
    pattern = source.get('pattern', None)

    clone_dir = os.path.join(UPDATE_DIR, name)
    if os.path.exists(clone_dir):
        shutil.rmtree(clone_dir)

    repo = Repo.clone_from(url, clone_dir)

    if pattern:
        files = [os.path.join(clone_dir, f) for f in os.listdir(clone_dir) if re.match(pattern, f)]
    else:
        files = glob.glob(os.path.join(clone_dir, '*.yar'))

    files_sha256 = [get_sha256_for_file(x) for x in files]

    return files, files_sha256


def replace_include(include, dirname, processed_files: Set[str]):
    include_path = re.match(r"include [\'\"](.{4,})[\'\"]", include).group(1)
    full_include_path = os.path.normpath(os.path.join(dirname, include_path))

    temp_lines = []
    if full_include_path not in processed_files:
        processed_files.add(full_include_path)
        with open(full_include_path, 'r') as include_f:
            lines = include_f.readlines()

        for i, line in enumerate(lines):
            if line.startswith("include"):
                lines, processed_files = replace_include(line, dirname, processed_files)
                temp_lines.extend(lines)
            else:
                temp_lines.append(line)

    return temp_lines, processed_files


def yara_update() -> None:
    """
    Using an update configuration file as an input, which contains a list of sources, download all the file(s).
    """
    if os.path.exists(UPDATE_CONFIGURATION_PATH):
        with open(UPDATE_CONFIGURATION_PATH, 'r') as yml_fh:
            update_config = yaml.safe_load(yml_fh)

    sources = update_config.get('sources', None)

    # Exit if no update sources given
    if 'sources' not in update_config.keys():
        exit()

    update_start_time = now_as_iso()
    sources = {source['name']: source for source in update_config['sources']}

    files_sha256 = []

    al_combined_yara_rules_dir = os.path.join(tempfile.gettempdir(), 'al_combined_yara_rules')
    if not os.path.exists(al_combined_yara_rules_dir):
        os.makedirs(al_combined_yara_rules_dir)

    # Go through each source and download file
    for source_name, source in sources.items():
        uri: str = source['uri']

        if uri.endswith('.git'):
            files, sha256 = git_clone_repo(source)
            if sha256:
                files_sha256.extend(sha256)
        else:
            previous_update = update_config.get('previous_update', None)
            files, sha256 = url_download(source, previous_update=previous_update)
            if sha256:
                files_sha256.append(sha256)

        processed_files = set()
        for file in files:
            # File has already been processed before, skip it to avoid duplication of rules
            if file in processed_files:
                continue

            file_basename = os.path.splitext(os.path.basename(file))[0]
            file_dirname = os.path.dirname(file)
            processed_files.add(os.path.normpath(file))
            with open(file, 'r') as f:
                f_lines = f.readlines()

            temp_lines = []
            for i, f_line in enumerate(f_lines):
                if f_line.startswith("include"):
                    lines, processed_files = replace_include(f_line, file_dirname, processed_files)
                    temp_lines.extend(lines)
                else:
                    temp_lines.append(f_line)

            # Save all rules from source into single file
            file_name = os.path.join(al_combined_yara_rules_dir, f"{source_name}_{file_basename}.yar")
            with open(file_name, 'w') as f:
                f.writelines(temp_lines)

    if not files_sha256:
        LOGGER.info('No YARA rule file(s) downloaded')
        exit()

    # new_hash = hashlib.md5(' '.join(sorted(files_sha256)).encode('utf-8')).hexdigest()
    #
    # # Check if the new update hash matches the previous update hash
    # if new_hash == update_config.get('previous_hash', None):
    #     # Update file(s) not changed, delete the downloaded files and exit
    #     shutil.rmtree(UPDATE_OUTPUT_PATH, ignore_errors=True)
    #     exit()

    LOGGER.info("YARA rule(s) file(s) successfully downloaded")

    server = update_config['ui_server']
    user = update_config['api_user']
    api_key = update_config['api_key']
    al_client = Client(server, apikey=(user, api_key), verify=False)

    yara_importer = YaraImporter(al_client)

    for x in os.listdir(al_combined_yara_rules_dir):
        source_name = os.path.splitext(os.path.basename(x))[0]

        tmp_dir = tempfile.mkdtemp(dir='/tmp')
        try:
            rules_file = os.path.join(tmp_dir, 'rules.yar')
            with open(rules_file, 'w') as f:
                f.write(open(os.path.join(al_combined_yara_rules_dir, x), 'r').read())

            _compile_rules(rules_file, source_name)
            yara_importer.import_file(rules_file, source_name)
        except Exception as e:
            raise e
        finally:
            shutil.rmtree(tmp_dir)

    # TODO: Download all signatures matching query and unzip received file to UPDATE_OUTPUT_PATH
    previous_update = update_config.get('previous_update', '')
    if al_client.signature.update_available(since=previous_update, sig_type='yara')['update_available']:
        LOGGER.info("AN UPDATE IS AVAILABLE TO DOWNLOAD")

        temp_zip_file = os.path.join(UPDATE_OUTPUT_PATH, 'temp.zip')
        al_client.signature.download(output=temp_zip_file, query="type:yara AND (status:TESTING OR status:DEPLOYED)")

        if os.path.exists(temp_zip_file):
            with ZipFile(temp_zip_file, 'r') as zip_f:
                zip_f.extractall(UPDATE_OUTPUT_PATH)

            os.remove(temp_zip_file)

        # Create the response yaml
        with open(os.path.join(UPDATE_OUTPUT_PATH, 'response.yaml'), 'w') as yml_fh:
            yaml.safe_dump(dict(
                previous_update=update_start_time,
                previous_hash='new_hash',
            ), yml_fh)


if __name__ == '__main__':
    yara_update()
