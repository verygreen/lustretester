from bs4 import BeautifulSoup
import pickle as pickle
import json
import requests
import os
from mymalooconfig import *

class maloo_poster(object):
    def __init__(self):
        self.base_api_url = "https://testing.whamcloud.com/api/"
        self.base_url = "https://testing.whamcloud.com/"
        self.username = a_username
        self.password = a_password
        self.session = None
        self.error = ""

    def authenticate(self):
        if self.session:
            return self.session

        try:
            with open("maloocookies.bin", "rb") as cookiefile:
                session = pickle.load(cookiefile)

            response = session.get(self.base_url + "retests")
            if response.status_code != requests.codes.ok:
                print("Failed to reuse session")
                os.unlink("maloocookies.bin")
            else:
                return session
        except:
            print("Failed to load session")

        session = requests.session()

        response = session.get(self.base_url + "signin")
        page = BeautifulSoup(response.text, features="lxml")
        tag = page.find('input', attrs = {'name':'authenticity_token'})
        token = tag['value']
        tag = page.find('input', attrs = {'name':'utf8'})
        utf8 = tag['value']
        payload = {
                'utf8': utf8.encode('utf-8'),
                'authenticity_token': token,
                'session[email]': self.username,
                'session[password]': self.password,
                'commit': "Sign in"
                }

        response = session.post(self.base_url + "sessions", data=payload, headers={'Content-type': 'application/x-www-form-urlencoded; charset=UTF-8'})
        if response.status_code != requests.codes.ok:
            return None

        try:
            with open("maloocookies.bin", "wb") as cookiefile:
                pickle.dump(session, cookiefile, pickle.HIGHEST_PROTOCOL)
        except:
            print("Failed to save session")

        self.session = session

        return session

    def get_session_token(self, session, url):
        # Get the testset first
        res = session.get(url)
        if res.status_code != requests.codes.ok:
            self.error = "Failed to fetch testset page"
            return False

        page = BeautifulSoup(res.text, features="lxml")
        token = None
        for item in page.select("meta"):
            if item.has_attr('name') and item.attrs['name'] == 'csrf-token':
                token = item['content']

        return token

    def post_buggable(self, testsetid, buggable_class, buggable_id, bug):
        session = self.authenticate()
        if not session:
            return False

        token = self.get_session_token(session, self.base_url + "test_sets/" + testsetid)
        if not token:
            return False

        payload = {
            'authenticity_token': token,
            'buggable_class': buggable_class,
            'buggable_id': buggable_id,
            'bug_upstream_id': bug
        }
        url = self.base_url + "buggable_links"
        #print("About to post ", payload)
        res = session.post(url, data=payload, headers={'Content-type': 'application/x-www-form-urlencoded; charset=UTF-8'})

        return res.status_code == requests.codes.ok

    def associate_bug_to_subtest(self, testsetid, subtestid, bug):
        return self.post_buggable(testsetid, 'SubTest', subtestid, bug)

    def associate_bug_to_testset(self, testsetid, bug):
        url = self.base_api_url + "sub_tests?test_set_id=" + testsetid

        res = requests.get(url, auth=(self.username, self.password))
        if res.status_code != requests.codes.ok:
            return self.post_buggable(testsetid, 'TestSet', testsetid, bug)

        if not res.json():
            self.error = "Error2 getting list of subtests"
            # XXX huh? can this even happen?
            #print(res.text)
            return False

        data = res.json().get('data', [])
        if not data:
            self.error = "Cannot load data " + res.text
            return False

        found = False
        for subtest in data:
            if subtest.get("status"):
                if subtest['status'] == 'CRASH':
                    found = True
                    break

        if found: # We found our crashed test
            return self.associate_bug_to_subtest(testsetid, subtest['id'], bug)

        return self.post_buggable(testsetid, 'TestSet', testsetid, bug)

    def associate_bug_by_url(self, url, bug, TESTLINE=None):
        if not url.startswith("https://testing.whamcloud.com/"):
            self.error = "Unknown url " + str(url)
            return False

        url = url.replace('https://testing.whamcloud.com/', '')
        if url.startswith('test_sets/'):
            testset = url.replace('test_sets/', '')
            return self.associate_bug_to_testset(testset, bug)
        elif url.startswith('test_sessions/'):
            testsession = url.replace('test_sessions/', '')
            testid = None
            # Now here's a bit of luck involved if we can translate
            # the testline into actual testset. If there was only one crash,
            # all is fine, but if not - we can try matching by test id.
            if TESTLINE:
                test = TESTLINE.split(" ")[0]
                if test != "rpc" and TESTLINE != "Module load":
                    # Now need to find if thi is a valid testid
                    url = self.base_api_url + "test_set_scripts?name=" + test
                    try:
                        res = requests.get(url, auth=(self.username, self.password))
                    except Exception as exc:
                        self.error = "Cannot get tests mappings"

                    if res.status_code != requests.codes.ok:
                        self.error = "Error getting tests mappings"

                    if res.json():
                        data = res.json().get('data', [])
                        if data:
                            if len(data) > 1:
                                self.error = "Huh, more than one entry for " + test
                                self.error += " " + str(data)
                            testid = data[0]['id']

            url = self.base_api_url + "test_sets?test_session_id=" + testsession
            if testid:
                url += "&test_set_script_id=" + testid
            try:
                res = requests.get(url, auth=(self.username, self.password))
            except Exception as exc:
                self.error = "Cannot get testsets for session id " + testsession
                return False

            if res.status_code != requests.codes.ok:
                self.error = "Error getting testsets for session id " + testsession
                return False

            if not res.json():
                self.error = "Invalid json for testsession " + testsession
                return False

            data = res.json().get('data', [])
            if not data:
                self.error = "Empty data for testsession " + testsession
                return False

            crashedcount = 0
            crashedtestset = None
            for testset in data:
                if testset['status'] == "CRASH":
                    crashedcount += 1
                    crashedtestset = testset['id']

            if crashedcount == 0:
                self.error = "Weird, did not find any crashed testsets here?"
                return False

            if crashedcount > 1:
                self.error = "Bad luck, there's more than one crashed testset"
                return False

            return self.associate_bug_to_testset(crashedtestset, bug)
        else:
            self.error = "Unknown url " + url
            return False
