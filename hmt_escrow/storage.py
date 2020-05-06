import os
import logging
import codecs
import hashlib
import json
import ipfshttpclient

from typing import Dict, Tuple
from eth_keys import keys
from p2p import ecies
from ipfshttpclient import Client

import boto3
from botocore.client import Config

SHARED_MAC_DATA = os.getenv(
    "SHARED_MAC",
    b'9da0d3721774843193737244a0f3355191f66ff7321e83eae83f7f746eb34350')

logging.getLogger("boto").setLevel(logging.INFO)
logging.getLogger("botocore").setLevel(logging.INFO)
logging.getLogger("boto3").setLevel(logging.INFO)

LOG = logging.getLogger("hmt_escrow.storage")
DEBUG = "true" in os.getenv("DEBUG", "false").lower()
LOG.setLevel(logging.DEBUG if DEBUG else logging.INFO)

IPFS_HOST = os.getenv("IPFS_HOST", "localhost")
IPFS_PORT = int(os.getenv("IPFS_PORT", 5001))

S3 = boto3.resource("s3", config=Config(signature_version="s3v4"))
ESCROW_BUCKETNAME = os.getenv("ESCROW_BUCKETNAME", "escrow-results")


def _connect(host: str, port: int) -> Client:
    try:
        IPFS_CLIENT = ipfshttpclient.connect(f'/dns/{host}/tcp/{port}/http')
        return IPFS_CLIENT
    except Exception as e:
        LOG.error("Connection with IPFS failed because of: {}".format(e))
        raise e


def _connect_s3():
    try:
        return boto3.client(
            "s3",
            aws_access_key_id=os.getenv("ESCROW_AWS_ACCESS_KEY_ID", "minio"),
            aws_secret_access_key=os.getenv("ESCROW_AWS_SECRET_ACCESS_KEY",
                                            "minio123"),
            endpoint_url=os.getenv("ESCROW_ENDPOINT_URL", "http://minio:9000"),
        )
    except Exception as e:
        LOG.error(f"Connection with S3 failed because of: {e}")
        raise e


def download(key: str, private_key: bytes, s3: bool = True) -> Dict:
    """Download a key, decrypt it, and output it as a binary string.

    >>> credentials = {
    ... 	"gas_payer": "0x1413862C2B7054CDbfdc181B83962CB0FC11fD92",
    ... 	"gas_payer_priv": "28e516f1e2f99e96a48a23cea1f94ee5f073403a1c68e818263f0eb898f1c8e5"
    ... }
    >>> pub_key = b"2dbc2c2c86052702e7c219339514b2e8bd4687ba1236c478ad41b43330b08488c12c8c1797aa181f3a4596a1bd8a0c18344ea44d6655f61fa73e56e743f79e0d"
    >>> job = Job(credentials=credentials, escrow_manifest=manifest)
    >>> (hash_, manifest_url) = upload(job.serialized_manifest, pub_key, False)
    >>> manifest_dict = download(manifest_url, job.gas_payer_priv, False)
    >>> manifest_dict == job.serialized_manifest
    True

    >>> job = Job(credentials=credentials, escrow_manifest=manifest)
    >>> (hash_, s3_hash) = upload(job.serialized_manifest, pub_key)
    >>> manifest_dict = download(s3_hash, job.gas_payer_priv)
    >>> manifest_dict == job.serialized_manifest
    True

    Args:
        key (str): This is the hash code returned when uploading.
        private_key (str): The private_key to decrypt this string with.

    Returns:
        Dict: returns the contents of the filename which was previously uploaded.

    Raises:
        Exception: if reading from IPFS fails.

    """
    if not s3:
        try:
            IPFS_CLIENT = _connect(IPFS_HOST, IPFS_PORT)
            LOG.debug("Downloading key: {}".format(key))
            ciphertext = IPFS_CLIENT.cat(key, timeout=30)
            msg = _decrypt(private_key, ciphertext)
        except Exception as e:
            LOG.warning(
                "Reading the key {!r} with private key {!r} with IPFS failed because of: {!r}"
                .format(key, private_key, e))
            raise e
        return json.loads(msg)

    try:
        LOG.debug("Downloading s3 key: {}".format(key))
        BOTO3_CLIENT = _connect_s3()
        response = BOTO3_CLIENT.get_object(Bucket=ESCROW_BUCKETNAME, Key=key)
        ciphertext = response['Body'].read()
        msg = _decrypt(private_key, ciphertext)
    except Exception as e:
        LOG.warning(
            "Reading the key {!r} with private key {!r} with S3 failed because of: {!r}"
            .format(key, private_key, e))
        raise e
    return json.loads(msg)


def upload(msg: Dict, public_key: bytes, s3: bool = True) -> Tuple[str, str]:
    """Upload and encrypt a string for later retrieval.
    This can be manifest files, results, or anything that's been already
    encrypted.

    >>> credentials = {
    ... 	"gas_payer": "0x1413862C2B7054CDbfdc181B83962CB0FC11fD92",
    ... 	"gas_payer_priv": "28e516f1e2f99e96a48a23cea1f94ee5f073403a1c68e818263f0eb898f1c8e5"
    ... }
    >>> pub_key = b"2dbc2c2c86052702e7c219339514b2e8bd4687ba1236c478ad41b43330b08488c12c8c1797aa181f3a4596a1bd8a0c18344ea44d6655f61fa73e56e743f79e0d"
    >>> job = Job(credentials=credentials, escrow_manifest=manifest)
    >>> (hash_, manifest_url) = upload(job.serialized_manifest, pub_key, False)
    >>> manifest_dict = download(manifest_url, job.gas_payer_priv, False)
    >>> manifest_dict == job.serialized_manifest
    True
    
    >>> job = Job(credentials=credentials, escrow_manifest=manifest)
    >>> (hash_, s3_hash) = upload(job.serialized_manifest, pub_key)
    >>> manifest_dict = download(s3_hash, job.gas_payer_priv)
    >>> manifest_dict == job.serialized_manifest
    True

    Args:
        msg (Dict): The message to upload and encrypt.
        public_key (bytes): The public_key to encrypt the file for.

    Returns:
        Tuple[str, str]: returns the contents of the filename which was previously uploaded.

    Raises:
        Exception: if adding bytes with IPFS fails.

    """
    try:
        manifest_ = json.dumps(msg, sort_keys=True)
    except Exception as e:
        LOG.error("Can't extract the json from the dict")
        raise e

    hash_ = hashlib.sha1(manifest_.encode('utf-8')).hexdigest()

    if not s3:
        try:
            IPFS_CLIENT = _connect(IPFS_HOST, IPFS_PORT)
            encrypted_msg = _encrypt(public_key, manifest_)
            key = IPFS_CLIENT.add_bytes(encrypted_msg)
            LOG.debug(f"Uploaded to IPFS, key: {key}")
            return hash_, key
        except Exception as e:
            LOG.warning(
                "Adding bytes with IPFS failed because of: {}".format(e))
            raise e

    try:
        BOTO3_CLIENT = _connect_s3()
        encrypted_msg = _encrypt(public_key, manifest_)
        BOTO3_CLIENT.put_object(Bucket=ESCROW_BUCKETNAME,
                                Key=hash_,
                                Body=encrypted_msg)
        LOG.debug(f"Uploaded to S3, used hash as key: {hash_}")
    except Exception as e:
        LOG.warning(
            f"Uploading with S3 failed with hash / key {hash_} because of: {e}"
        )
    return hash_, hash_


def _decrypt(private_key: bytes, msg: bytes) -> str:
    """Use ECIES to decrypt a message with a given private key and an optional MAC.

    >>> priv_key = "28e516f1e2f99e96a48a23cea1f94ee5f073403a1c68e818263f0eb898f1c8e5"
    >>> pub_key = b"2dbc2c2c86052702e7c219339514b2e8bd4687ba1236c478ad41b43330b08488c12c8c1797aa181f3a4596a1bd8a0c18344ea44d6655f61fa73e56e743f79e0d"
    >>> msg = "test"
    >>> _decrypt(priv_key, _encrypt(pub_key, msg)) == msg
    True

    Using a wrong public key to decrypt a message results in failure.
    >>> false_pub_key = b"74c81fe41b30f741b31185052664a10c3256e2f08bcfb20c8f54e733bef58972adcf84e4f5d70a979681fd39d7f7847d2c0d3b5d4aead806c4fec4d8534be114"
    >>> _decrypt(priv_key, _encrypt(false_pub_key, msg)) == msg
    Traceback (most recent call last):
    p2p.exceptions.DecryptionError: Failed to verify tag

    Args:
        private_key (bytes): The private_key to decrypt the message with.
        msg (bytes): The message to be decrypted.

    Returns:
        str: returns the plaintext equivalent to the originally encrypted one.

    """
    priv_key = keys.PrivateKey(codecs.decode(private_key, 'hex'))
    e = ecies.decrypt(msg, priv_key, shared_mac_data=SHARED_MAC_DATA)
    return e.decode('utf-8')


def _encrypt(public_key: bytes, msg: str) -> bytes:
    """Use ECIES to encrypt a message with a given public key and optional MAC.

    >>> priv_key = "28e516f1e2f99e96a48a23cea1f94ee5f073403a1c68e818263f0eb898f1c8e5"
    >>> pub_key = b"2dbc2c2c86052702e7c219339514b2e8bd4687ba1236c478ad41b43330b08488c12c8c1797aa181f3a4596a1bd8a0c18344ea44d6655f61fa73e56e743f79e0d"
    >>> msg = "test"
    >>> _decrypt(priv_key, _encrypt(pub_key, msg)) == msg
    True

    Args:
        public_key (bytes): The public_key to encrypt the message with.
        msg (str): The message to be encrypted.

    Returns:
        bytes: returns the cryptotext encrypted with the public key.

    """
    pub_key = keys.PublicKey(codecs.decode(public_key, 'hex'))
    msg_bytes = msg.encode('utf-8')
    return ecies.encrypt(msg_bytes, pub_key, shared_mac_data=SHARED_MAC_DATA)


if __name__ == "__main__":
    import doctest
    from test_manifest import manifest
    from job import Job
    doctest.testmod()
