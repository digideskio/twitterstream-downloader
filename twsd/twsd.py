#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function

import argparse
import datetime
import json
import logging
import os.path
import sys
import time
import urllib2

import requests
from requests_oauthlib import OAuth1
from urlparse import parse_qs

import db
from auth import Keychain
from yn import query_yes_no

# Set up logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
screen_handler = logging.StreamHandler(sys.stderr)
screen_handler.setLevel(logging.INFO)
screen_handler.setFormatter(formatter)
logger.addHandler(screen_handler)


# TODO use logging module
# TODO move OAUTH parameters to external config file (json, YAML or conf)
# TODO better recovery (backfill)

DESCRIPTION = """Download tweets in realtime using the Twitter Streaming API.

"""

EPILOG = """Requires Python Requests and Python Requests Oauthlib."""

URL = 'https://stream.twitter.com/1.1/statuses/{0}.json'

S_AUTH1 = """
To authorize the app, please go to {0} and follow the process.
Please enter the PIN to complete the authorization process.
"""
S_AUTH2 = """
Authorization details:
USER_KEY: {0},
USER_SECRET: {1}
"""

def authorize(consumer_key, consumer_secret):
    oauth = OAuth1(consumer_key, client_secret=consumer_secret)
    request_token_url = "https://api.twitter.com/oauth/request_token"
    r = requests.post(url=request_token_url, auth=oauth)
    credentials = parse_qs(r.content)
    resource_owner_key = credentials.get('oauth_token')[0]
    resource_owner_secret = credentials.get('oauth_token_secret')[0]
    authorize_url = "https://api.twitter.com/oauth/authorize?oauth_token="
    authorize_url = authorize_url + resource_owner_key
    print(S_AUTH1.format(authorize_url))
    verifier = raw_input('PIN: ')

    oauth = OAuth1(consumer_key, consumer_secret,
            resource_owner_key, resource_owner_secret, verifier=verifier)
    access_token_url = "https://api.twitter.com/oauth/access_token"
    r = requests.post(url=access_token_url, auth=oauth)
    credentials = parse_qs(r.content)
    at = credentials.get('oauth_token')[0]
    ats = credentials.get('oauth_token_secret')[0]
    #print(S_AUTH2.format(resource_owner_key, resource_owner_secret))
    return at, ats

class TwitterStreamCrawler(object):
    def __init__(self, base_filename, user_key, user_secret, app_key,
            app_secret):
        self.base_filename = base_filename
        self._db_instance = db.FileAppendDb(base_filename)
        self._auth = OAuth1(app_key, app_secret, user_key, user_secret, 
                      signature_type='auth_header')
        file_handler = logging.FileHandler(base_filename+".log")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    def each_tweet(self, line):
        line = line.strip()
        if line:
            self._db_instance.save(line)
            #self._db_instance.sync()
            try:
                tweet = json.loads(line)
                if 'limit' in tweet:
                    n_tweets = tweet['limit']['track']
                    logger.warning("LIMIT: %s tweets retained", n_tweets)
            except ValueError:
                logger.error("Not valid JSON on line: %s", line)

    def request_stream(self, url, data):
        count = 0
        start_time = datetime.datetime.now()
        db_instance = self._db_instance
        r = requests.post(url, data=data, auth=self._auth, stream=True, timeout=self.timeout)
        each_tweet = self.each_tweet
        for line in r.iter_lines():
            each_tweet(line)
            count += 1
            if not count % 100:
                now = datetime.datetime.now()
                delta = now - start_time
                rate = float(count) / delta.total_seconds()
                print("Total tweets", count, "\tRate", rate) # Change to logger.
                start_time = datetime.datetime.now()
                count = 0

    def receive(self, endpoint, data, print_tweets=False):
        url = URL.format(endpoint)
        while True:
            try:
                logger.info("Requesting stream: %s. Params: %s", url, data)
                self.request_stream(url, data)
                # TODO print_tweets
            except (KeyboardInterrupt, SystemExit):
                # TODO clean outfile?
                logger.info("Shutting down (manual shutdown).")
                logging.shutdown()
                sys.exit(0)
            # Handle network timeout
            except requests.exceptions.Timeout as e:
                timeout = self.timeout
                delay = self.delay # TODO introduce exp backoff
                logger.info("Request timed out (timeout=%s). Waiting and retrying (delay=%s).", timeout, delay)
                time.sleep(delay)

def make_keychain():
    k = Keychain()

    # Try to load a previous keychain
    home = os.path.expanduser("~")
    basename = ".twsd.auth"
    authfname = os.path.join(home, basename)
    try:
        k.load(authfname)
    except IOError: # File doesn't exist
        conscred = query_yes_no("Do you have a CONSUMER KEY, CONSUMER SECRET pair?")
        if not conscred:
            print("Create a new application on https://dev.twitter.com/apps and try again.")
            sys.exit(0)
        else:
            k = Keychain()
            ck = raw_input("Input your CONSUMER KEY: ")
            cs = raw_input("Input your CONSUMER SECRET: ")
        k.set_consumer(ck, cs)
        usercred = query_yes_no("Do you have an ACCESS TOKEN, ACCESS TOKEN SECRET pair?")
        if usercred:
            at = raw_input("Input your ACCESS TOKEN: ")
            ats = raw_input("Input your ACCESS TOKEN SECRET: ")
        else:
            at, ats = authorize(ck, cs)
        k.set_user(at, ats)

    # Save the keychain for the next time
    k.save(authfname)
    return k

def main():
    parser = argparse.ArgumentParser(description="""Download twitter streams using the
    Streaming API""", epilog=EPILOG)
    parser.add_argument('endpoint', help='Method of the Streaming API to use',
            choices=('filter', 'sample', 'firehose', 'authorize'))
    parser.add_argument('fileprefix', help='output json to the specified file',
            action='store') 
    parser.add_argument('-p', help="""add a method parameter ('name=value')""",
            metavar="PARNAME=PARVAL", action='append')
    parser.add_argument('-o', '--print', help='print every tweet', action='store_true')
    parser.add_argument('--timeout', default=30,
        help='Streaming timeout in seconds (default 30)',
        action='store', type=int, metavar="SECS")
    parser.add_argument('--delay', default=10,
        help='Sleep delay in seconds (default 10)',
        action='store', type=int, metavar="SECS")
    args = parser.parse_args()

    k = make_keychain()

    endpoint = args.endpoint
    data = {}
    if args.p:
        data = dict([i.split('=') for i in args.p])
    if endpoint == 'filter':
        assert set(data).intersection(('track', 'locations', 'follow'))

    filename = args.fileprefix
    ck, cs = k.get_consumer()
    at, ats = k.get_user()
    api = TwitterStreamCrawler(filename, at, ats, ck, cs)
    api.timeout = args.timeout
    api.delay = args.delay
    api.receive(endpoint, data, print_tweets=True)

if __name__ == "__main__":
    main()
