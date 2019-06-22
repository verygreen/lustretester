#!/usr/bin/python3
import os
import sys
from pprint import pprint
import mailbox
import psycopg2
import cgi
import cgitb

from mycrashanalyzer import extract_crash_from_dmesg_string, is_known_crash, check_untriaged_crash

cgitb.enable()

print("Content-type: text/html")
print()

def print_found_bug(bug, extrainfo):
    template = """<html><head><title>Found!</title></head>
<body>
<H2>This is a known bug <a href=\"https://jira.whamcloud.com/browse/{BUG}\">{BUG}</a></H2>
Extrainfo: {EXTRAINFO}
"""
    all_items = {'BUG':bug, 'EXTRAINFO':extrainfo}
    return template.format(**all_items)

def print_untriaged_link(newid, numreports):
    template = """<html><head><title>Found!</title></head>
<body>
<H2>We have seen this report {NUMREPORT} times.</H2>
But it was not triaged it yet.<br>
<a href=\"https://knox.linuxhacker.ru/crashdb_ui_external.py.cgi?newid={NEWID}\">You can review the report id {NEWID} here</a>
"""
    all_items = {'NUMREPORT':numreports, 'NEWID':newid}
    return template.format(**all_items)

def print_empty_form():
    template = """<html><head><title>Enter kernel log data</title></head>
<body>
<H2>Please paste your dmesg output here</H2>
<form method=\"post\">
<textarea name=\"dmesg\" rows=40 cols=80></textarea>
<input type=\"submit\" name=\"dmesg_submit\" value=\"Look it up!\"/>
</form>
</body>
</html>
"""
    return template

dbconn = psycopg2.connect(dbname="crashinfo", user="crashinfo", password="blah", host="localhost")

if __name__ == "__main__":
    form = cgi.FieldStorage()
    if not form or not form.getfirst('dmesg'):
        result = print_empty_form()
    else:
        dmesg = form.getfirst('dmesg')

        (lasttest, entirecrash, lasttestlogs, crashtrigger, function, abbreviatedbt) = extract_crash_from_dmesg_string(dmesg)
        if not abbreviatedbt and not "Inexact backtrace" in entirecrash:
            print("<pre>cannot find bt")
            pprint(entirecrash)
            result = ""
        else: 
            (result, extrainfo) = is_known_crash(lasttest, crashtrigger, function, abbreviatedbt, entirecrash, lasttestlogs, DBCONN=dbconn)
            if not result:
                # check it in triage
                (newid, numreports) = check_untriaged_crash(lasttest, crashtrigger, function, abbreviatedbt, entirecrash, lasttestlogs)
                # 0 id means error
                if newid is None:
                    result = "Db problem, try again sometime"
                elif not newid:
                    result = "We have not seen that backtrace before"
                else:
                    result = print_untriaged_link(newid, numreports)
                    print("Filed as %d and it was seen %d times before" % (newid, numreports))
            else:
                result = print_found_bug(result, extrainfo)

    print(result)

dbconn.close()
