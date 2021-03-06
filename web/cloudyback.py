#!/usr/bin/python
#
# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import bisect
import functools
import glob
import hashlib
import itertools
import math
import os
import re
import time
from collections import Counter, defaultdict, OrderedDict
from datetime import datetime, timezone

import choix
import numpy as np
from tqdm import tqdm

from . import sgf_utils
from . import utils
from .cloudygo import CloudyGo


def all_games(cloudy, bucket, model, game_type='full'):
    assert CloudyGo.LEELA_ID not in bucket, "LZ uses _get_games_from_model"
    assert CloudyGo.KATAGO_ID not in bucket, "KataGo uses _get_games_from_model"

    if bucket in CloudyGo.MINIGO_TS:
        assert False, (bucket, "Should use _get_games_from_ts")

    # NOTE: An older version of cloudygo would load games in two passes
    # Parsing clean then full games this gave some flexibility but at
    # the cost of overall speed now the code tries to do both parses
    # in one pass falling back to simple_parse when debug information is
    # not present.
    # TODO: Support fallback to clean dir
    path = os.path.join(cloudy.sgf_path(bucket), model, game_type)
    if not os.path.exists(path):
        return []
    return glob.glob(os.path.join(path, '*.sgf'))


def get_eval_parts(filename):
    assert filename.endswith('.sgf'), filename

    # TODO(sethtroisi): What is a better way to determine if this is part
    # of a run (e.g. LZ, MG) or a test eval dir?

    SEP = 1000

    # MG: 1527290396-000241-archer-vs-000262-ship-long-0.sgf
    # LZ: 000002-88-fast-vs-18-fast-202.sgf
    # TODO compile this
    is_run = re.match(
        r'^[0-9]+-[0-9]+-[a-z-]+-vs-[0-9]+-[a-z-]+-[0-9]+\.sgf$',
        filename)
    if is_run:
        # make sure no dir_eval games end up here
        raw = re.split(r'[._-]+', filename)
        nums = [int(part) for part in raw if part.isnumeric()]
        assert len(nums) == 4, '{} => {}'.format(filename, raw)
        assert max(nums[1:]) < SEP, nums
        multed = sum(num * SEP ** i for i, num in enumerate(nums[::-1]))
        return [multed, nums[1], nums[2]]

    MAX_EVAL_NUM = 2 ** 60
    num = int(hashlib.md5(filename.encode()).hexdigest(), 16)
    return [num % MAX_EVAL_NUM, 0, 0]


def update_models(cloudy, bucket, only_create=False):
    if CloudyGo.LEELA_ID in bucket:     # LEELA-HACK
        model_glob = os.path.join(cloudy.model_path(bucket), 'LZ[0-9]*_[0-9a-f_]*')
    elif CloudyGo.KATAGO_ID in bucket:  # KATAGO-HACK
        model_glob = os.path.join(cloudy.model_path(bucket), 'KataGo-b[0-9]*c[0-9]*-s[0-9]*')
    else:
        model_glob = os.path.join(cloudy.model_path(bucket), '*.meta')
    model_filenames = glob.glob(model_glob)

    existing_models = cloudy.get_models(bucket)
    existing = set(m[0] for m in existing_models)

    model_inserts = []
    model_stat_inserts = []
    for model_filename in sorted(model_filenames):
        raw_name = os.path.basename(model_filename).replace('.meta', '')

        if (CloudyGo.LEELA_ID in bucket) or (CloudyGo.KATAGO_ID in bucket):
            # LEELA-HACK and KATAGO-HACK
            # Note: this is brittle but I can't think of how to get model_id
            exist = [m for m in existing_models if raw_name.startswith(m[1])]
            assert len(exist) == 1, (model_filename, raw_name, lz)
            exist = exist[0]

            model_id = exist[0]
            display_name = exist[1]
            raw_name = raw_name
            sgf_name = raw_name
            model_num = exist[5]
        else:
            model_num, model_name = raw_name.split('-', 1)
            model_id = CloudyGo.bucket_salt(bucket) + int(model_num)
            display_name = raw_name
            raw_name = raw_name
            sgf_name = raw_name
            # TODO add slow lookup here

        if only_create and model_id in existing:
            continue

        last_updated = int(time.time())
        training_time_m = 120
        creation = int(os.path.getmtime(model_filename))

        # TODO Check if existing count is small.
        if (last_updated - creation > (CloudyGo.FAST_UPDATE_HOURS * 3600)):
            continue

        num_games = cloudy.query_db(
            'SELECT count(*), sum(has_stats) from games WHERE model_id = ?',
            (model_id,))

        assert len(num_games) == 1
        num_games, num_stats_games = num_games[0]
        num_stats_games = num_stats_games or 0

        num_eval_games = cloudy.query_db(
            'SELECT sum(games) from eval_models '
            'WHERE model_id_1 = ? AND model_id_2 = 0',
            (model_id,))
        num_eval_games = num_eval_games[0][0] or 0

        model = (
            model_id,
            display_name, raw_name, sgf_name,
            bucket, int(model_num),

            # generated fields
            last_updated, creation, training_time_m,
            num_games, num_stats_games, num_eval_games
        )
        assert len(model) == 12, model
        model_inserts.append(model)

        currently_processed = cloudy.query_db(
            'SELECT max(stats_games) FROM model_stats WHERE model_id = ?',
            (model_id,))
        currently_processed = currently_processed[0][0] or 0
        if num_games == currently_processed:
            continue

        opening_name = str(model_id) + '-favorite-openings.png'
        opening_file = os.path.join(
            cloudy.INSTANCE_PATH, 'openings', opening_name)
        opening_sgf = sgf_utils.board_png(
            CloudyGo.bucket_to_board_size(bucket),
            '',  # setup
            cloudy.get_favorite_openings(model_id, num_games),
            opening_file,
            force_refresh=True)

        # NOTE: if table is altered * may return unexpected order
        games = cloudy.query_db(
            'SELECT * from games WHERE model_id = ?',
            (model_id,))

        for perspective in ['all', 'black', 'white']:
            is_all = perspective == 'all'
            is_black = perspective == 'black'

            # ASSUMPTION: every game has a result
            wins = games
            if not is_all:
                wins = [game for game in games if game[4] == is_black]

            wins_by_resign = len([1 for game in wins if '+R' in game[5]])
            sum_wins_result = sum(float(game[5][2:]) for game in wins
                                  if '+R' not in game[5])

            resign_rates = Counter(game[17] for game in wins)
            # TOO NOISE: v13 had one rate per cluster, v14 was dynamic.
            # if len(resign_rates) > 2 and CloudyGo.LEELA_ID not in bucket:
            #     if perspective == 'all':
            #         print('{} has multiple Resign rates: {}'.format(
            #             raw_name, resign_rates))
            resign_rates.pop(-1, None)  # remove -1 if it's present

            # Note resign_rates are negative, max gets the value closest to zero.
            resign_rate = max(resign_rates.keys()) if resign_rates else -1
            assert resign_rate < 0, resign_rate

            # TODO count leela holdouts but ignore resign problem.
            holdouts = [game for game in wins if abs(game[17]) == 1]
            holdout_resigns = [
                game for game in holdouts if '+R' in game[5]]
            assert len(holdout_resigns) == 0, holdout_resigns

            bad_resigns = 0
            for game in holdouts:
                black_won = game[4]

                # bleakest eval is generally negative for black and positive for white
                black_would_resign = game[18] < resign_rate
                white_would_resign = -game[19] < resign_rate

                if black_won:
                    if black_would_resign:
                        bad_resigns += 1
                else:
                    if white_would_resign:
                        bad_resigns += 1

            model_stat_inserts.append((
                model_id, perspective,
                num_games, num_stats_games,
                len(wins), wins_by_resign,
                len(wins) - wins_by_resign, sum_wins_result,
                len(holdouts), bad_resigns,

                sum(game[7] for game in wins),  # num_moves
                sum(game[11] + game[12]
                    for game in wins),  # both sides visits
                sum(game[13] + game[14]
                    for game in wins),  # both sides early visits
                sum(game[15] + game[16]
                    for game in wins),  # both sides unluckiness
                opening_sgf if is_all else '',  # favorite_openings
            ))

    db = cloudy.db()

    cur = db.executemany(
        'DELETE FROM models WHERE model_id = ?',
        set((model[0],) for model in model_inserts))
    removed = cur.rowcount

    cur = db.executemany(
        'DELETE FROM model_stats WHERE model_id = ?',
        set((model[0],) for model in model_stat_inserts))
    removed_stats = cur.rowcount

    print('updated {}:  {} existing, {}|{} removed, {}|{} inserts'.format(
        bucket,
        len(existing),
        removed, removed_stats,
        len(model_inserts), len(model_stat_inserts)))

    cloudy.insert_rows_db('models', model_inserts)
    cloudy.insert_rows_db('model_stats', model_stat_inserts)

    db.commit()

    return len(model_inserts) + len(model_stat_inserts)


def process_game(data):
    game_path, game_num, filename, model_id = data
    results = sgf_utils.parse_game(game_path)
    if not results: return None
    sgf_model, *result = results
    assert result, (game_path, results)

    return ((game_path, sgf_model),
            (game_num + (model_id, filename) + tuple(result)))


def update_model_names(cloudy):
    MODEL_SRC = 'model'

    names = defaultdict(set)
    aliases = {}

    def verify_unique(alias, bucket, model_id):
        key = (bucket, alias)
        test = aliases.get(key)
        if test not in (None, model_id):
            # NOTE:
            #   v16 000001 and 000002 both map to model.ckpt-1024
            #   v14 000319 and 000318 both map to model.ckpt-331136
            print('Proposed alias {} => {} already mapped to {}'
                .format(key, model_id, test))
            assert False, (key, test)
        aliases[key] = model_id

    def consider_alias(alias, bucket, model_id, source):
        verify_unique(alias, bucket, model_id)
        if alias not in names[model_id]:
            inserts.append((alias, bucket, model_id, source))
            names[model_id].add(alias)

    query = cloudy.query_db(
        'SELECT model_id, bucket, name FROM name_to_model_id')
    for model_id, bucket, alias in query:
        names[model_id].add(alias)
        verify_unique(alias, bucket, model_id)

    model_names = cloudy.query_db(
        'SELECT model_id, bucket, display_name, raw_name, sgf_name '
        'FROM models '
        'ORDER BY model_id')
    for model_id, bucket, name1, name2, name3 in model_names:
        for name in (name1, name2, name3):
            verify_unique(name, bucket, model_id)

    inserts = []
    for model_id, bucket, name1, name2, name3 in model_names:
        for name in (name1, name2, name3):
            consider_alias(name, bucket, model_id, MODEL_SRC)

            # LEELA-HACK: also add short name.
            if re.match(r'^[0-9a-fA-F]{64}$', name):
                short_name = name[:8]
                consider_alias(short_name, bucket, model_id, MODEL_SRC)
            if re.match(r'^LZ[0-9]*_[0-9a-fA-F]{8}$', name):
                short_name = name.split('_')[1][:8]
                consider_alias(short_name, bucket, model_id, MODEL_SRC)

    # MINIGO-HACK
    for model_id, bucket, display_name, raw_name, sgf_name in model_names:
        if bucket not in CloudyGo.MINIGO_TS:
            continue

        # Check if checkpoint name already looked up.
        cur = any(n.startswith(CloudyGo.MODEL_CKPT) for n in names[model_id])
        if len(raw_name) > 3 and not cur:
            # TODO: extract to function
            path = os.path.join(cloudy.model_path(bucket), raw_name)
            if os.path.isfile(path + '.meta'):
                print ('Slow lookup of checkpoint step for',
                    model_id, raw_name)
                import tensorflow.train as tf_train
                ckpt = tf_train.load_checkpoint(path)
                step = ckpt.get_tensor('global_step')

                new_name = CloudyGo.MODEL_CKPT + str(step)
                consider_alias(new_name, bucket, model_id, MODEL_SRC)
                print ("\t", model_id,raw_name, " =>  ", new_name)

    if inserts:
        cloudy.insert_rows_db('name_to_model_id', inserts)
        cloudy.db().commit()
        print('Updated {} model names'.format(len(inserts)))


def update_bucket_ranges(cloudy, bucket_names):
    buckets = cloudy.query_db('SELECT bucket from bucket_model_range')
    buckets = set([b[0] for b in buckets])

    for bucket in bucket_names:
        if bucket not in buckets:
            model_range = CloudyGo.bucket_model_range(bucket)
            print("Adding {} => {}".format(bucket, model_range))
            cloudy.insert_rows_db(
                'bucket_model_range',
                [(bucket,) + model_range])
            cloudy.db().commit()


def _model_guesser(filename, model_mtimes, model_ids):
    game_time = int(filename.split('-', 1)[0])
    game_time -= CloudyGo.MINIGO_GAME_LENGTH
    assert CloudyGo.MIN_TS < game_time < CloudyGo.MAX_TS, game_time
    model_num = bisect.bisect(model_mtimes, game_time, 1) - 1
    assert model_num >= 0, model_num
    return model_ids[model_num]


def _game_paths_to_to_process(
        bucket, existing,
        model_lookup,
        game_paths,
        max_inserts):

    bucket_salt = CloudyGo.bucket_salt(bucket)
    to_process = []

    for game_path in sorted(game_paths):
        # TODO keep inner folder with timestamp run, LZ

        filename = os.path.basename(game_path)
        game_num = CloudyGo.get_game_num(bucket_salt, filename)
        has_stats = '/full/' in game_path

        # Skip if already processed UNLESS was processed without stats.
        current = existing.get(game_num, None)
        if current or (current == False and not has_stats):
            continue

        existing[game_num] = has_stats

        # TODO can this be moved to process_sgf_model_names?
        # MINIGO-HACK, this would be easy otherwise
        model_id = model_lookup(filename)
        to_process.append((game_path, game_num, filename, model_id))

        if len(to_process) >= max_inserts:
            break
    return to_process


def _get_update_games_block_folders(cloudy, bucket, max_inserts):
    # Folders numbered incrementally.
    # At the current time assumes these aren't updated regurally.

    base_paths = os.path.join(cloudy.sgf_path(bucket), '*')

    # KATAGO-HACK use step number instead of all of dir name
    if CloudyGo.KATAGO_ID in bucket:
        sort_by_func = lambda p: int(os.path.basename(p).split('s')[-1])
    else:
        sort_by_func = lambda p: int(os.path.basename(p))

    block_dirs = sorted(glob.glob(base_paths), key=sort_by_func)
    if len(block_dirs) == 0:
        return

    print ("\t{} folders: {}".format(
        len(block_dirs),
        utils.list_preview(list(map(os.path.basename, block_dirs)), 2)))

    # TODO: add some fast update hack.
    model_range = CloudyGo.bucket_model_range(bucket)
    existing = cloudy._get_games_from_models(model_range)
    print("{} existing games (FAST-UPDATE please)".format(len(existing)))

    # These get fixed after process_game by process_sgf_model_names
    model_lookup = lambda filename: 0

    for block_dir in block_dirs:
        update_name = bucket + "/" + os.path.basename(block_dir) + "/"

        game_paths = glob.glob(os.path.join(block_dir, '*.sgf'))
        to_process = _game_paths_to_to_process(
            bucket, existing, model_lookup, game_paths, max_inserts)
        yield update_name, to_process, len(existing)


def _get_update_games_time_dir(cloudy, bucket, max_inserts):
    model_range = CloudyGo.bucket_model_range(bucket)
    models = cloudy.get_models(bucket)

    model_mtimes = [model[7] for model in models]
    model_ids = [model[0] for model in models]
    model_lookup = functools.partial(
        _model_guesser,
        model_mtimes=model_mtimes,
        model_ids=model_ids)

    # NOTE: this code goes first so we know what timestamp range to load.
    to_update = OrderedDict()
    for d_type in ['full', 'clean']:
        base_paths = os.path.join(cloudy.sgf_path(bucket), d_type, '*')
        time_dirs = sorted(glob.glob(base_paths))
        # FAST UPDATE HACK
        will_update = time_dirs[-CloudyGo.FAST_UPDATE_HOURS:]
        if len(will_update) > 0:
            to_update[d_type] = will_update
        print ("\t{}, {} folders (updating {}): {}".format(
            d_type,
            len(time_dirs),
            len(will_update),
            utils.list_preview(list(map(os.path.basename, time_dirs)), 1)))

    if len(to_update) == 0:
        return

    def get_folder_ts(func):
        all_updates = [d for v in to_update.values() for d in v]
        folder = func(map(os.path.basename, all_updates))
        dt = datetime.strptime(folder, '%Y-%m-%d-%H')
        return int(dt.replace(tzinfo=timezone.utc).timestamp())

    min_ts = get_folder_ts(min) - 5
    max_ts = get_folder_ts(max) + 3605

    existing = cloudy._get_games_from_ts(model_range, (min_ts, max_ts))
    print("{} existing games in last {}ish hours ({} to {})".format(
        len(existing), CloudyGo.FAST_UPDATE_HOURS, min_ts, max_ts))

    # TODO find a way to rsync faster
    for d_type, time_dirs in to_update.items():
        for time_dir in time_dirs:
            name = os.path.basename(time_dir)

            game_paths = glob.glob(os.path.join(time_dir, '*.sgf'))
            to_process = _game_paths_to_to_process(
                bucket, existing, model_lookup, game_paths, max_inserts)
            yield name, to_process, len(existing)


def _get_update_games_model(cloudy, bucket, max_inserts):
    skipped = []
    for model in cloudy.get_models(bucket):
        # FAST UPDATE HACK: only newest day of model folders are updated.
        # Check if directory mtime is recent, if not skip
        raw_name = model[2]
        test_d = os.path.join(cloudy.sgf_path(bucket), raw_name, 'full')
        m_time = os.path.getmtime(test_d) if os.path.exists(test_d) else 0
        if model[9] > 0 and model[6] > m_time + 3600 * CloudyGo.FAST_UPDATE_HOURS:
            # If greater than FAST_UPDATE_HOURS since it was created, skip
            skipped.append(model[5])
            continue

        name = '{}-{}'.format(model[5], model[1])
        model_id = model[0]
        model_lookup = lambda: model_id

        existing = cloudy._get_games_from_model(model_id)
        # TODO: Support update from clean if so desired.
        game_paths = cloudy.all_games(bucket, raw_name)
        to_process = _game_paths_to_to_process(
            bucket, existing, model_lookup, game_paths, max_inserts)

        yield name, to_process, len(existing)
    if len(skipped) > 0:
        print('skipped {}, {}'.format(
            len(skipped), utils.list_preview(skipped)))


def map_and_filter(cloudy, funct, items, unit="it"):
    length = len(items)
    mapper = cloudy.pool.imap if cloudy.pool else itertools.imap
    mapped = mapper(funct, items)

    if length < 100:
        results = list(mapped)
    else:
        results = list(tqdm(mapped, unit=unit, total=length))

    broken = results.count(None)
    if broken > 10:
        print("{} Broken".format(broken))

    return list(filter(None.__ne__, results))


def update_games(cloudy, bucket, max_inserts):
    # This is REALLY SLOW because it's potentially >1M items
    # loop by model to avoid huge globs and commits
    updates = 0
    bucket_salt = CloudyGo.bucket_salt(bucket)

    if bucket in CloudyGo.MINIGO_TS:
        games_source = _get_update_games_time_dir(cloudy, bucket, max_inserts)
    elif CloudyGo.LEELA_ID in bucket:
        games_source = _get_update_games_block_folders(cloudy, bucket, max_inserts)
    elif CloudyGo.KATAGO_ID in bucket:
        games_source = _get_update_games_block_folders(cloudy, bucket, max_inserts)
    else:
        games_source = _get_update_games_model(cloudy, bucket, max_inserts)

    for model_name, to_process, len_existing in games_source:
        if len(to_process) > 0:
            print("About to process {} games of {}".format(
                len(to_process), model_name))

            new_games = cloudy.map_and_filter(CloudyGo.process_game, to_process)

            # Post-process PB/PW to model_id (when folder/filename is ambiguous)
            new_games = cloudy.process_sgf_model_names(bucket, new_games)

            # Some Games were processed as clean may now have stats data.
            cloudy.insert_rows_db('games', new_games, allow_existing=True)
            cloudy.db().commit()

            result = '{}: {} existing, {} inserts'.format(
                model_name, len_existing, len(new_games))

            if len(new_games):
                print(result)
            updates += len(new_games)
    return updates


def update_position_eval(cloudy, filename, bucket, model_id, group, name):
    board_size = CloudyGo.bucket_to_board_size(bucket)

    with open(filename) as eval_file:
        values = list(map(str.strip, eval_file.read().strip().split(',')))

    # Files start with model_id (idx)
    test = values.pop(0)
    assert test in str(model_id), \
        '{} not in {}'.format(test, model_id)

    value = values.pop(0)
    policy = None
    n = None
    sgf = None

    if group == 'policy':
        assert len(values) in (9*9+1, 19*19+1), (group, filename)

        render = (name == 'empty' and group == 'policy')
        filename = '-'.join([str(model_id), group, name + '.png'])
        filepath = os.path.join(
            cloudy.INSTANCE_PATH, 'openings', filename)

        data = [(cord, float(policy), 0)
                for cord, policy in enumerate(values)]

        sgf = cloudy.render_position_eval(
            bucket, model_id,
            group, name, data,
            filename=filepath if render else None)
    else:
        assert group == 'pv', '{} {} {}'.format(
            group, len(values), filename)
        assert len(values) % 2 == 0, values

        data = []
        for i in range(0, len(values), 2):
            cord = int(values[i])
            count = int(values[i+1])
            data.append((cord, 0, count))

        sgf = cloudy.render_position_eval(
            bucket, model_id, group, name, data)

    sgf = sgf_utils.canonical_sgf(board_size, sgf)
    insert = (model_id, -2, group, name, policy, value, n, sgf)

    cloudy.db().execute(
        'DELETE FROM position_eval_part '
        'WHERE model_id = ? AND type = ? AND name = ?',
        (model_id, group, name))

    cloudy.insert_rows_db('position_eval_part', [insert])
    cloudy.db().commit()


def process_eval(data):
    eval_path, filename, eval_num, model_id_1, model_id_2 = data
    result = sgf_utils.parse_game_simple(eval_path, include_players=True)
    if not result:
        return None
    return (eval_num, filename, model_id_1, model_id_2) + result


def sanitize_player_name(name):
    # See oneoff/leela-all-to-dirs.sh
    name = re.sub(
        r'Leela\s*Zero\s*([0-9](\.[0-9]+)*)?\s*(networks)?\s*', '', name)
    name = re.sub(r'([0-9a-f]{8})[0-9a-f]{56}', r'\1', name)
    return name


def process_sgf_model_names(cloudy, bucket, records):
    # Used for self-play games
    model_range = CloudyGo.bucket_model_range(bucket)
    bucket_salt = model_range[0]
    name_to_num = dict(cloudy.query_db(
        'SELECT name, model_id FROM name_to_model_id '
        'WHERE model_id BETWEEN ? AND ?',
        model_range))
    new_names = []

    def get_name(raw_name):
        name = CloudyGo.sanitize_player_name(raw_name)

        if name in name_to_num:
            return name_to_num[name]

        # LEELA-HACK: leela-zero-v3-eval hack.
        is_lz_name = re.match(r'^LZ([0-9]+)_[0-9a-f]{8,}', name)
        if bucket.startswith('leela') and is_lz_name:
            return bucket_salt + int(is_lz_name.group(1))

        if name == '' and 'Leela Zero' in raw_name:
            # Early LZ models => 0
            return model_range[0]

        # KATAGO-HACK (not hack)
        assert CloudyGo.KATAGO_ID not in name, name

        return None
        #TODO assert False

    # process game returns
    # ((game_path, sgf_model),
    #  (game_num + (model_id, filename) + parse_result))

    new_records = []
    for record in records:
        model_id = record[1][2]
        if model_id != 0:
            assert model_range[0] <= model_id <= model_range[1], (
                model_id, bucket, model_range)
            new_records.append(record[1])
        else:
            game_path = record[0][0]
            sgf_model = record[0][1]
            test_model_id = get_name(sgf_model)
            if not test_model_id:
                print("Skipping", record[1][3], sgf_model)
            else:
                new_record = list(record[1])
                new_record[2] = test_model_id
                new_records.append(tuple(new_record))

    return new_records


def process_sgf_names(cloudy, bucket, records):
    # Used for eval_games
    SGF_SRC = 'sgf'

    model_range = CloudyGo.bucket_model_range(bucket)
    bucket_salt = model_range[0]
    name_to_num = dict(cloudy.query_db(
        'SELECT name, model_id FROM name_to_model_id '
        'WHERE model_id BETWEEN ? AND ?',
        model_range))
    new_names = []

    def ckpt_num(name):
        if name.startswith(CloudyGo.MODEL_CKPT):
            return int(name[len(CloudyGo.MODEL_CKPT):])
        return None

    def get_or_add_name(name):
        name = CloudyGo.sanitize_player_name(name)

        if name in name_to_num:
            return name_to_num[name]

        # MINIGO-HACK: figure out how to plumb is_sorted here
        if (bucket.startswith('v') and re.match(r'[0-9]{6}-([a-zA-Z-]+)', name)):
            return bucket_salt + int(name.split('-', 1)[0])

        # LEELA-HACK: leela-zero-v3-eval hack.
        is_lz_name = re.match(r'^LZ([0-9]+)_[0-9a-f]{8,}', name)
        if bucket.startswith('leela') and is_lz_name:
            return bucket_salt + int(is_lz_name.group(1))

        # KATAGO-HACK (not hack)
        assert CloudyGo.KATAGO_ID not in name, name

        # NOTE: static "models" (e.g. gnu go) get a special id range.
        if name in CloudyGo.SPECIAL_EVAL_NAMES:
            index = CloudyGo.SPECIAL_EVAL_NAMES.index(name)
            start = bucket_salt + CloudyGo.SPECIAL_EVAL_START
            number = start + index
            name_to_num[name] = number
            new_names.append((name, bucket, number, SGF_SRC))
            return number

        # MINIGO-HACK: bucket ~= 'v10-19x19', name ~= 'model.ckpt.123'
        if bucket.startswith('v') and ckpt_num(name):
            # These should really be handled by update_model_names
            # This mostly handles eval record, and update_model_names
            # does validation, so this is okay, but not great.

            num = ckpt_num(name)
            previous = set(ckpt_num(other) for other in name_to_num.keys())
            count_less = sum(1 for p in previous if p and p < num)
            # ckpt-0 and ckpt-1 both mean 000000-bootstrap.
            # awkwardly count_less filters ckpt-0 so the count is correct.
            number = bucket_salt + count_less
            name_to_num[name] = number
            new_names.append((name, bucket, number, SGF_SRC))
            print("get_or_add_name ckpt:", name, number)
            return number

        first_eval_model = bucket_salt + CloudyGo.CROSS_EVAL_START
        keys = set(name_to_num.values())
        for test_id in range(first_eval_model, model_range[1]):
            if test_id not in keys:
                name_to_num[name] = test_id
                new_names.append((name, bucket, test_id, SGF_SRC))
                return test_id
        assert False

    if bucket in CloudyGo.ALL_EVAL_BUCKETS:
       #Look up all model names so we can differentiate v10-250 vs v12-250.
        name_to_bucket = cloudy.query_db(
            'SELECT name, bucket '
            'FROM name_to_model_id '
            'WHERE bucket like "v%"')
        name_to_buckets = defaultdict(list)
        for name, b in name_to_bucket:
            name_to_buckets[name].append(b)
        print("\t{} names, {} unique".format(
            len(name_to_bucket), len(name_to_buckets)))

    def bucket_from_name(filename, PB, PW):
        # Attempt to find what bucket PB and PW belong too for ALL_EVAL_BUCKETS
        first_folder = filename.split('/')[0]
        if first_folder in CloudyGo.MINIGO_TS:
            prefix = first_folder + '/'
            return prefix + PB, prefix + PW
        else:
            # Probably cross-eval look for vXX-MMM-vs-vYY-NNN
            match = CloudyGo.CROSS_EVAL_BUCKET_MODEL.search(filename)
            if match:
                xB, xN, yB, yN = match.groups()
                assert len(xB) in (2,3) and len(yB) in (2,3), (xB, yB)
                xB += '-19x19'
                yB += '-19x19'

                if xB == yB:
                    return xB + "/" + PB, xB + "/" + PW

                # Find what buckets this model name belongs too.
                pB_B = name_to_buckets[PB]
                pW_B = name_to_buckets[PW]

                x_is_b = xB in pB_B
                x_is_w = xB in pW_B
                assert x_is_b or x_is_w, filename
                y_is_b = yB in pB_B
                y_is_w = yB in pW_B
                assert y_is_b or y_is_w, filename

                if x_is_b and y_is_w and not (x_is_w and y_is_b):
                    return xB + "/" + PB, yB + "/" + PW
                elif x_is_w and y_is_b and not (x_is_b and y_is_w):
                    return yB + "/" + PB, xB + "/" + PW
                else:
                    print ("Utter confusion", filename, PB, PW, pB_B, pW_B)

        return None, None

    new_records = []
    for record in records:
        # Eval records
        if bucket_salt == record[2] == record[3]:
            new_record = list(record[:-2])
            PB, PW = record[-2:]
            # HACK: add the bucket name to avoid name collusions
            if bucket in CloudyGo.ALL_EVAL_BUCKETS:
                PB, PW = bucket_from_name(record[1], PB, PW)
                if PB == None or PW == None:
                    print("Skipping", record)
                    continue

            new_record[2] = get_or_add_name(PB)
            new_record[3] = get_or_add_name(PW)

        elif ckpt_num(record[1]) is not None:
            # MINIGO-HACK
            new_record = list(record)
            model = record[1]
            new_record[1] = get_or_add_name(model)
        else:
            # Eval game that has well formed model_ids.
            # TODO plumb is_sorted around and don't set model_ids
            new_record = list(record[:-2])

        assert new_record, record
        new_records.append(tuple(new_record))

    # NOTE: this depends on someone else calling db.commit()
    if new_names:
        cloudy.insert_rows_db('name_to_model_id', new_names)

    return new_records


def update_eval_games(cloudy, bucket):
    eval_dir = cloudy.eval_path(bucket)
    if not os.path.exists(eval_dir):
        return 0

    bucket_salt = CloudyGo.bucket_salt(bucket)

    existing = cloudy._get_eval_games(bucket)
    evals_to_process = []
    new_eval_nums = set()

    eval_games = glob.glob(
        os.path.join(eval_dir, '**', '*.sgf'),
        recursive=True)

    # sort by newest first
    eval_games = sorted(eval_games, reverse=True)
    for eval_path in eval_games:
        # NOTE: we want to keep the YYYY-MM-DD folder part
        partial_path = eval_path.replace(eval_dir + '/', '')
        filename = os.path.basename(eval_path)

        # NOTE: for files from ringmaster we use partial_path which
        # is generally the ctl filename.
        if re.match('[0-9]+_[0-9]+\.sgf', filename):
            filename = partial_path

        eval_num, m1, m2 = get_eval_parts(filename)
        if eval_num in existing:
            continue
        assert eval_num not in new_eval_nums, (eval_num, filename)
        new_eval_nums.add(eval_num)

        # Minigo eval games have white before black
        white_model = bucket_salt + m1
        black_model = bucket_salt + m2

        evals_to_process.append(
            (eval_path,
             partial_path,
             eval_num,
             black_model,
             white_model))

        if len(evals_to_process) >= CloudyGo.MAX_INSERTS:
            break

    new_evals = []
    if len(evals_to_process) > 0:
        print()
        new_evals = cloudy.map_and_filter(
            CloudyGo.process_eval,
            evals_to_process,
            unit="eval games")

        # Post-process PB/PW to model_id (when filename is ambiguous)
        new_evals = cloudy.process_sgf_names(bucket, new_evals)

        if new_evals:
            cloudy.insert_rows_db('eval_games', new_evals)
            cloudy.db().commit()

        print('eval_games: {} existing, {} inserts'.format(
            len(existing), len(new_evals)))
    return len(new_evals)


def update_eval_models(cloudy, bucket):
    model_range = CloudyGo.bucket_model_range(bucket)

    eval_games = cloudy.query_db(
        'SELECT '
        '   model_id_1, '
        '   model_id_2, '
        '   black_won '
        'FROM eval_games '
        'WHERE model_id_1 BETWEEN ? AND ?',
        model_range)

    if len(eval_games) < 10:
        return 0

    total_games = len(eval_games)
    model_nums = sorted(
        set(e[0] for e in eval_games) |
        set(e[1] for e in eval_games)
    )

    previous_rating = dict(cloudy.query_db(
        'SELECT model_id_1, rankings '
        'FROM eval_models '
        'WHERE model_id_1 BETWEEN ? AND ? and model_id_2 = 0',
        model_range))

    max_model_num = max(
        [m_n for m_n in model_nums
            if (m_n % CloudyGo.SALT_MULT) < CloudyGo.SPECIAL_EVAL_START],
        default=0)
    print('loaded {} evals for {} models ({} to {})'.format(
        total_games, len(model_nums),
        min(model_nums, default=-1),
        max_model_num))

    print('\t{} evals, ratings {:.0f} to {:.0f}'.format(
        len(previous_rating),
        min(previous_rating.values(), default=0),
        max(previous_rating.values(), default=1)))

    ratings = CloudyGo.get_eval_ratings(
        model_nums, eval_games, previous_rating)
    assert 0 not in ratings, ratings.keys()

    # black games, black wins,    white games, white wins
    model_evals = defaultdict(lambda: [0, 0, 0, 0])

    def increment_record(record, played_black, black_won):
        record[0 if played_black else 2] += 1
        if played_black == black_won:
            record[1 if played_black else 3] += 1

    for d in eval_games:
        black, white, black_won = d

        # Update by model
        increment_record(model_evals[(white, 0)], False, black_won)
        increment_record(model_evals[(black, 0)], True, black_won)

        # Update by pairing
        increment_record(model_evals[(white, black)], False, black_won)
        increment_record(model_evals[(black, white)], True, black_won)

    records = []
    for (m1, m2), (m1_b, m1_b_wins, m1_w, m1_w_wins) in model_evals.items():
        m1_rating = ratings[m1]
        m2_rating = ratings[m2] if m2 != 0 else m1_rating
        records.append([
            m1, m2,
            (m1_rating[0] + m2_rating[0]) / 2,
            # Note V std err should be combine (sum std_err^2)^(1/2)
            (m1_rating[1] + m2_rating[1]) / 2,

            m1_b + m1_w,
            m1_b, m1_b_wins,
            m1_w, m1_w_wins,
        ])

    # Delete old eval_models and add new eval games
    cur = cloudy.db().execute(
        'DELETE FROM eval_models WHERE model_id_1 BETWEEN ? AND ?',
        model_range)
    removed = cur.rowcount

    cloudy.insert_rows_db('eval_models', records)
    cloudy.db().commit()

    cloudy.db().executemany(
        'UPDATE models SET num_eval_games = ? WHERE model_id = ?',
        [(k[0], d[0] + d[2]) for k, d in model_evals.items() if k[1] == 0])
    cloudy.db().commit()

    return len(records) - removed


def get_eval_ratings(model_nums, eval_games, priors):

    # Map model_nums to a contigious range.
    ordered = sorted(set(model_nums))
    new_num = {}
    for i, m in enumerate(ordered):
        new_num[m] = i

    # Transform priors (previous_ratings) (~elo~) back to Luce Spectral
    # Elo conversion
    elo_mult = 400 / math.log(10)
    init = None
    if priors:
        avg = np.average(list(priors.values()))
        init = [(priors.get(o, avg) - avg) / elo_mult for o in ordered]

    def ilsr_data(eval_game):
        p1, p2, black_won = eval_game
        p1 = new_num[p1]
        p2 = new_num[p2]
        assert 0 <= p1 <= 10000, p1
        assert 0 <= p2 <= 10000, p2

        return (p1, p2) if black_won else (p2, p1)

    pairs = list(map(ilsr_data, eval_games))
    ilsr_param = choix.ilsr_pairwise(
        len(ordered),
        pairs,
        initial_params=init,
        alpha=0.0001,
        max_iter=1000)

    hessian = choix.opt.PairwiseFcts(pairs, penalty=.1).hessian(ilsr_param)
    std_err = np.sqrt(np.diagonal(np.linalg.inv(hessian)))

    min_rating = min(ilsr_param)
    ratings = {}
    for model, param, err in zip(ordered, ilsr_param, std_err):
        ratings[model] = (elo_mult * (param - min_rating), elo_mult * err)

    return ratings
