import sys
import os
import time
import yaml
import json
from mytestdatadb import process_results
from mytestdatadb import process_warning
import dateutil.parser
import cPickle as pickle
from pprint import pprint

def get_base_url(wkitem):
    url = fsconfig['http_server']
    offset = fsconfig['root_path_offset']
    cut = len(offset)
    path = wkitem.artifactsdir
    if path[:cut] != offset:
        return "Error path substitution, server misconfiguration!"
    url += path[cut:]
    return url

def get_url_for_test(wkitem, testinfo):
    return get_base_url(wkitem) + testinfo.get('ResultsDir', '').replace(wkitem.artifactsdir, '')

with open("./fsconfig.json") as fsconfig_file:
    fsconfig = json.load(fsconfig_file)

for savefile in sorted(os.listdir("donewith"), key=lambda x: int(x.split(".")[0])):
    if ".pickle" not in savefile:
        continue

    with open("donewith/" + savefile, "rb") as blah:
        try:
            workitem = pickle.load(blah)
        except:
            print("Cannot load: ", savefile)
            continue
        if not workitem.BuildDone or workitem.BuildError:
            print("file %s buildid %d - no build info" % (savefile, workitem.buildnr))
            continue

        branch = workitem.change['branch']
        if workitem.change.get('change_id'): # because "branchwide" was added later
            gerritnr = int(workitem.change.get("_number"))
        else:
            gerritnr = None

        print("Loaded " + str(workitem.buildnr) + " branch " + branch + " changenr " + str(gerritnr))

        for testitem in workitem.initial_tests + workitem.tests:
            resultsdir = testitem.get("ResultsDir")
            if not resultsdir:
                continue
            # We only need failed tests
            if not testitem.get('Finished'):
                continue

            try:
                link = workitem.get_url_for_test(testitem)
            except:
                link = get_url_for_test(workitem, testitem)
                print("Cannot get link for " + resultsdir + " made " + link)

            fstype = testitem['fstype']

            if testitem.get("Failed"):
                yamlfile = resultsdir + '/results.yml'
                if not os.path.exists(yamlfile):
                    continue

                try:
                    with open(yamlfile, "r") as fl:
                        fldata = fl.read()
                        testresults = yaml.safe_load(fldata.replace('"', ''))
                except (OSError, ImportError) as e:
                    print("Error loading " + yamlfile + " : " + str(e))
                    continue
                if testitem.get("Warnings"):
                    testtime = dateutil.parser.parse(testresults['TestGroup']['submission'])
                    tname = testitem.get("name", testitem['test'])
                    warnings = testitem['Warnings'].split(")(")
                    for warning in warnings:
                        warning.replace('(', '')
                        warning.replace(')', '')
                        unique = process_warning(tname, warning, workitem.change, link, fstype, testtime=testtime)

                new, old = process_results(testresults, workitem, link, fstype)
                if len(new):
                    print("Got new unique results:")
                    pprint(new)
                if len(old):
                    print("Got seen results:")
                    pprint(old)

