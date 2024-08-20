# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Integration code for libFuzzer fuzzer."""

import subprocess
import os
import shutil
from fuzzers import utils


def build():
    """Build benchmark."""
    # With LibFuzzer we use -fsanitize=fuzzer-no-link for build CFLAGS and then
    # /usr/lib/libFuzzer.a as the FUZZER_LIB for the main fuzzing binary. This
    # allows us to link against a version of LibFuzzer that we specify.
    cflags = ['-fsanitize=fuzzer-no-link']
    utils.append_flags('CFLAGS', cflags)
    utils.append_flags('CXXFLAGS', cflags)

    os.environ['CC'] = 'clang'
    os.environ['CXX'] = 'clang++'
    os.environ['FUZZER_LIB'] = '/usr/lib/libFuzzer.a'

    utils.build_benchmark()


def fuzz(input_corpus, output_corpus, target_binary):
    """Run fuzzer. Wrapper that uses the defaults when calling
    run_fuzzer."""
    run_fuzzer(input_corpus, output_corpus, target_binary)

def prepare_empty_corpus(input_corpus):
    if os.path.exists(input_corpus):
        shutil.rmtree(input_corpus)
    os.makedirs(input_corpus)
    with open(os.path.join(input_corpus, 'a'), 'wb') as f:
        f.write(b'a')


def run_fuzzer(input_corpus, output_corpus, target_binary, extra_flags=None):
    """Run fuzzer."""
    if extra_flags is None:
        extra_flags = []
    prepare_empty_corpus(input_corpus)
    # Seperate out corpus and crash directories as sub-directories of
    # |output_corpus| to avoid conflicts when corpus directory is reloaded.
    crashes_dir = os.path.join(output_corpus, 'crashes')
    output_corpus = os.path.join(output_corpus, 'corpus')
    os.makedirs(crashes_dir)
    os.makedirs(output_corpus)

    # Enable symbolization if needed.
    # Note: if the flags are like `symbolize=0:..:symbolize=1` then
    # only symbolize=1 is respected.
    for flag in extra_flags:
        if flag.startswith('-focus_function'):
            if 'ASAN_OPTIONS' in os.environ:
                os.environ['ASAN_OPTIONS'] += ':symbolize=1'
            else:
                os.environ['ASAN_OPTIONS'] = 'symbolize=1'
            if 'UBSAN_OPTIONS' in os.environ:
                os.environ['UBSAN_OPTIONS'] += ':symbolize=1'
            else:
                os.environ['UBSAN_OPTIONS'] = 'symbolize=1'
            break

    flags = [
        '-print_final_stats=1',
        # `close_fd_mask` to prevent too much logging output from the target.
        '-close_fd_mask=3',
        # Run in fork mode to allow ignoring ooms, timeouts, crashes and
        # continue fuzzing indefinitely.
        '-fork=1',
        '-ignore_ooms=1',
        '-ignore_timeouts=1',
        '-ignore_crashes=1',
        '-entropic=1',
        '-keep_seed=1',
        '-cross_over_uniform_dist=1',
        '-entropic_scale_per_exec_time=1',

        # Don't use LSAN's leak detection. Other fuzzers won't be using it and
        # using it will cause libFuzzer to find "crashes" no one cares about.
        '-detect_leaks=0',

        # Store crashes along with corpus for bug based benchmarking.
        f'-artifact_prefix={crashes_dir}/',
    ]
    flags += extra_flags
    if 'ADDITIONAL_ARGS' in os.environ:
        flags += os.environ['ADDITIONAL_ARGS'].split(' ')
    dictionary_path = utils.get_dictionary_path(target_binary)
    #if dictionary_path:
    #    flags.append('-dict=' + dictionary_path)

    command = [target_binary] + flags + [output_corpus, input_corpus]
    print('[run_fuzzer] Running command: ' + ' '.join(command))
    subprocess.check_call(command)