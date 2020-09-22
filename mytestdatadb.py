from datetime import datetime
from datetime import timedelta
import dateutil.parser
from pprint import pprint
import psycopg2

def process_warning(testname, warning, change, resultlink, fstype, testtime=None):
    unique = False
    dbconn = None

    branch = change['branch']
    if change.get('change_id'): # because "branchwide" was added later
        gerritid = int(change.get("_number"))
    else:
        gerritid = None

    if not testtime:
        testtime = datetime.now()

    branch_next = branch.endswith("-next")

    if branch_next:
        branch = branch.replace("-next", "")

    try:
        dbconn = psycopg2.connect(dbname="testinfo", user="testinfo", password="blah1", host="localhost", Time=None)
        cur = dbconn.cursor()
        # First let's see if it's a branch wide warning
        cur.execute("SELECT id, created_at FROM warnings WHERE test = %s AND fstype = %s AND warning = %s AND GerritID is NULL AND branch = %s AND created_at >= %s - interval '30' day ORDER BY created_at desc", (testname, fstype, warning, branch, testtime))
        if cur.rowcount == 0: # Only saw it for this gerrit id or never
            unique = True
        if gerritid:
            cur.execute("INSERT INTO warnings(created_at, branch, GerritID, test, warning, Link, fstype) VALUES (%s, %s, %s, %s, %s, %s, %s)", (testtime, branch, gerritid, testname, warning, resultlink, fstype))
        elif not branch_next: # don't want new -next branch results stored
            cur.execute("INSERT INTO warnings(created_at, branch, test, warning, Link, fstype) VALUES (%s, %s, %s, %s, %s, %s)", (testtime, branch, testname, warning, resultlink, fstype))

        dbconn.commit()
        cur.close()
    except psycopg2.DatabaseError as e:
        print("Cannot insert new warning entry " + str(e))
    finally:
        if dbconn:
            dbconn.close()

    return unique

def process_one(testname, subtestname, error, duration, branch, gerritid, resultlink, testtime, fstype):
    """ Try to match one failire from db """
    unique = False
    msg = ""
    branch_next = branch.endswith("-next")
    dbconn = None

    if branch_next:
        branch = branch.replace("-next", "")

    try:
        dbconn = psycopg2.connect(dbname="testinfo", user="testinfo", password="blah1", host="localhost", Time=None)
        cur = dbconn.cursor()
        # First let's see if it's a branch wide failure
        cur.execute("SELECT id, created_at FROM failures WHERE test = %s AND subtest = %s AND fstype = %s AND error = %s AND GerritID is NULL AND branch = %s AND created_at >= %s - interval '30' day ORDER BY created_at desc", (testname, subtestname, fstype, error, branch, testtime))

        # Only do further search if it was a review request
        if cur.rowcount == 0 and gerritid: # We have not seen this for this branch last 30 days
            cur.execute("SELECT DISTINCT ON (GerritID) GerritID FROM failures WHERE test = %s AND subtest = %s AND fstype = %s AND error = %s AND GerritID <> %s AND branch = %s ORDER BY GerritID desc, id LIMIT 100", (testname, subtestname, fstype, error, gerritid, branch))
            if cur.rowcount == 0: # Only saw it for this gerrit id or never
                unique = True
                # Check all other branches
                cur.execute("SELECT count(id), count(DISTINCT GerritID), count(DISTINCT branch) FROM failures WHERE test = %s AND subtest = %s AND fstype = %s AND error = %s AND created_at >= %s - interval '30' day", (testname, subtestname, fstype, error, testtime))
                # count must be 1!
                row = cur.fetchone()
                msg = "NEW unique failure for this branch in the last 30 days, and was seen %d times across %d other branches %d reviews" % (row[0], row[2], row[1])

                # See if this is a blacklisted result
                cur.execute("SELECT id FROM blacklisted WHERE test = %s AND subtest = %s AND fstype = %s AND %s LIKE CONCAT(blacklisted.errorstart, %s)", (testname, subtestname, fstype, error, '%' ))
                if cur.rowcount:
                    unique = False
                    msg = "blacklisted variable error message"

            else: # Saw it for other gerrit IDs but not for the base branch
                unique = True
                msg = "Seen in reviews:"
                for row in cur.fetchall():
                    msg += " %d" % (row[0])
        else:
            if cur.rowcount:
                # Since it's a generic failure we'll record it without gerritid
                # so it counts against overall statistics
                # The link would still lead corectly to this review.
                gerritid = None
                lasthit = cur.fetchone()[1]
                msg = "%d fails in 30d" % (cur.rowcount)
                if lasthit.replace(tzinfo=None) < datetime.now() - timedelta(days=2):
                    msg += ", last  %s" % (lasthit.strftime('%Y-%m-%d'))
            else:
                unique = True
                msg = "NEW unseen before"

            # See if this is a blacklisted result
            cur.execute("SELECT id FROM blacklisted WHERE test = %s AND subtest = %s AND fstype = %s AND %s LIKE CONCAT(blacklisted.errorstart, %s)", (testname, subtestname, fstype, error, '%' ))
            if cur.rowcount:
                unique = False
                msg = "blacklisted variable error message"


        # Because you cannot insert NULL into integer field apparently
        if gerritid:
            cur.execute("INSERT INTO failures(created_at, branch, GerritID, test, subtest, duration, error, Link, fstype) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", (testtime, branch, gerritid, testname, subtestname, duration, error, resultlink, fstype))
        elif not branch_next: # don't want new -next branch results stored
            cur.execute("INSERT INTO failures(created_at, branch, test, subtest, duration, error, Link, fstype) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", (testtime, branch, testname, subtestname, duration, error, resultlink, fstype))

        dbconn.commit()
        cur.close()
    except psycopg2.DatabaseError as e:
        print("Cannot insert new failure entry " + str(e))
    finally:
        if dbconn:
            dbconn.close()

    return (unique, msg)

def process_results(results, workitem, resultlink, fstype):
    """ Go over all test results, log failures and see if they were seen before """
    UniqMsgs = []
    KnownMsgs = []

    branch = workitem.change['branch']
    if workitem.change.get('change_id'): # because "branchwide" was added later
        gerritnr = int(workitem.change.get("_number"))
    else:
        gerritnr = None

    try:
        for yamltest in results.get('Tests', []):
            if yamltest.get('submission'):
                testtime = dateutil.parser.parse(yamltest.get('submission'))
            else:
                testtime = datetime.now()

            for subtest in yamltest.get('SubTests', []):
                try:
                    if subtest.get('status', '') == "FAIL":
                        error = subtest.get('error', '').replace('\\', '')
                        subtestname = subtest['name']
                        testname = yamltest.get('name', '')
                        testduration = subtest.get('duration', 0)
                        unique, msg = process_one(testname, subtestname, error, testduration, branch, gerritnr, resultlink, testtime, fstype)
                        element = "%s(%s)" % (subtestname, msg)
                        if unique:
                            UniqMsgs.append(element)
                        else:
                            KnownMsgs.append(element)

                except TypeError as e:
                    pass # Nothing to do here for a broken result
    except TypeError as e:
        pass # Nothing to do here for a broken result

    return (UniqMsgs, KnownMsgs)
