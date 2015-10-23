import os
import time
import bisect
import hashlib
import binascii
import RandomIO
import partialhash
import psutil
import json

try:
    # For Python 3.0 and later
    from urllib.request import urlopen
except ImportError:
    # Fall back to Python 2's urllib2
    from urllib2 import urlopen

from datetime import datetime
from dataserv_client import control
from dataserv_client import common


logger = common.logging.getLogger(__name__)


class Builder:

    def __init__(self, address, shard_size, max_size, min_free_size, 
                 on_generate_shard=None, use_folder_tree=False):
        self.target_height = int(max_size / shard_size)
        self.address = address
        self.shard_size = shard_size
        self.max_size = max_size
        self.min_free_size = min_free_size
        self.use_folder_tree = use_folder_tree
        self.on_generate_shard = on_generate_shard

    @staticmethod
    def sha256(content):
        """Finds the SHA-256 hash of the content."""
        content = content.encode('utf-8')
        return hashlib.sha256(content).hexdigest()

    def _build_all_seeds(self, height):
        """Includes seed for height 0."""
        seed = self.sha256(self.address)
        seeds = [seed]
        for i in range(height):
            seed = self.sha256(seed)
            seeds.append(seed)
        return seeds

    def build_seeds(self, height):
        """Deterministically build seeds."""
        return self._build_all_seeds(height)[:height]

    def build_seed(self, height):
        """Deterministically build a seed."""
        return self._build_all_seeds(height).pop()

    def _get_shard_path(self, store_path, seed, create_needed_folders=False):
        if self.use_folder_tree:
            folders = os.path.join(*control.util.chunks(seed, 3))
            store_path = os.path.join(store_path, folders)
            if create_needed_folders:
                control.util.ensure_path_exists(store_path)
        return os.path.join(store_path, seed)

    def generate_shard(self, seed, store_path, cleanup=False):
        """
        Save a shard, and return its SHA-256 hash.

        :param seed: Seed pased to RandomIO to generate file.
        :param store_path: What path to store the file.
        :param cleanup: Delete the file after generation.
        :return: SHA-256 hash of the file.
        """

        # save the shard
        path = self._get_shard_path(store_path, seed,
                                    create_needed_folders=True)
        try:
            RandomIO.RandomIO(seed).genfile(self.shard_size, path)
        except IOError as e:
            msg = "Failed to write shard, will try once more! '{0}'"
            logger.error(msg.format(repr(e)))
            time.sleep(2)
            RandomIO.RandomIO(seed).genfile(self.shard_size, path)

        # get the file hash
        with open(path, 'rb') as f:
            file_hash = hashlib.sha256(f.read()).hexdigest()

        # remove file if requested
        if cleanup:
            os.remove(path)

        return file_hash

    def filter_to_resume_point(self, store_path, enum_seeds):
        """
        Binary search to find the proper place to resume.

        :param store_path: What path to the files are stored at.
        :param enum_seeds: List of seeds to check.
        :return:
        """
        class HackedCompareObject(str):
            def __gt__(hco_self, seed):
                path = self._get_shard_path(store_path, seed)
                return os.path.exists(path)

        seeds = [seed for num, seed in enum_seeds]
        index = bisect.bisect_left(seeds, HackedCompareObject())

        logger.info("Resuming from height {0}".format(index))
        return index

    def build(self, store_path, workers=1, cleanup=False, rebuild=False, repair=False):
        """
        Fill the farmer with data up to their max.

        :param store_path: What path to store the file.
        :param cleanup: Delete the file after generation.
        :param rebuild: Re-generate the shards.
        """

        generated = {}
        pool = control.Thread.ThreadPool(workers)

        enum_seeds = list(enumerate(self.build_seeds(self.target_height)))
        last_height = 0
        if not rebuild:
            last_height = self.filter_to_resume_point(store_path, enum_seeds)
            
            # rebuild bad or missing shards
            if repair:
                for shard_num, seed in enum_seeds[:last_height]:
                    path = self._get_shard_path(store_path, seed)
                    if not (os.path.exists(path) and os.path.getsize(path) == self.shard_size):
                        logger.info("Repeair seed {0} height {1}.".format(seed, shard_num))
                        pool.add_task(self.generate_shard, seed, store_path, cleanup)
                pool.wait_completion()

        for shard_num, seed in enum_seeds[last_height:]:
            try:
                if psutil.disk_usage(store_path).free - (self.shard_size * (pool.active_count() + 1)) < self.min_free_size:
                    msg="Minimum free disk space reached ({0}) for '{1}'."
                    logger.info(msg.format(self.min_free_size, store_path))
                    last_height = shard_num
                    break

                file_hash = pool.add_task(self.generate_shard, seed, store_path, cleanup)

                generated[seed] = file_hash
                logger.info("Saving seed {0} with SHA-256 hash {1}.".format(
                    seed, file_hash
                ))

                last_height = shard_num + 1
                if self.on_generate_shard:
                    self.on_generate_shard(shard_num + 1, False)

            except KeyboardInterrupt:
                last_height = shard_num + 1
                logger.warning("Caught KeyboardInterrupt, finishing workers")
                break

        pool.wait_completion()
        if self.on_generate_shard:
            self.on_generate_shard(last_height, True)

        return generated

    def clean(self, store_path):
        """
        Delete shards from path.

        :param store_path: Path the shards are stored at.
        """

        seeds = self.build_seeds(self.target_height)
        for shard_num, seed in enumerate(seeds):
            path = self._get_shard_path(store_path, seed)
            if os.path.exists(path):
                os.remove(path)

    def checkup(self, store_path):
        """
        Make sure the shards exist.

        :param store_path: Path the shards are stored at.
        :return True if all shards exist, False otherwise.
        """

        seeds = self.build_seeds(self.target_height)
        for shard_num, seed in enumerate(seeds):
            path = self._get_shard_path(store_path, seed)
            if not os.path.exists(path):
                return False
        return True

    def last_btc_index(self):
        """last Bitcoin index"""
        url = 'https://blockexplorer.com/api/status?q=getBlockCount'
        result = json.loads(urlopen(url).read().decode('utf8'))
        return result['blockcount']

    def btc_block(self, index):
        """last Bitcoin hash"""
        url = 'https://blockexplorer.com/api/block-index/' + str(index)
        result = json.loads(urlopen(url).read().decode('utf8'))
        return result['blockHash']

    def audit(self, store_path):
        """audit one block"""
        btc_index = self.last_btc_index()
        btc_hash = self.btc_block(btc_index)
        block_pos = btc_index % 1000
        block_size = common.DEFAULT_BLOCK_SIZE
        
        seeds = self.build_seeds((block_pos + 1) * block_size)
        
        #check if the block is complete
        for seed in seeds[(block_pos * block_size):]:
            path = self._get_shard_path(store_path, seed)
            if not (os.path.exists(path) and os.path.getsize(path) == self.shard_size):
                logger.info("Audit for block {0} - {1} failed.".format(block_pos * block_size, block_pos * (block_size + 1)))
                return 0

        #generate audit response
        audit_hash = ""
        for seed in seeds[(block_pos * block_size):]:
            path = self._get_shard_path(store_path, seed)
            with open(path, 'rb') as file:
                audit_hash += hashlib.sha256(file.read()).hexdigest()
        return hashlib.sha256(audit_hash.encode('utf-8')).hexdigest()
