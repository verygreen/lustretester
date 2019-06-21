#!/usr/bin/python
import os
import sys
from pprint import pprint
import psycopg2
import cgi
import cgitb
from mycrashanalyzer import add_known_crash
import mymaloo_bugreporter
cgitb.enable()


print "Content-type: text/html"
print

dbconn = psycopg2.connect(dbname="crashinfo", user="crashinfo", password="blah", host="localhost")

def xstr(s):
    if s is None:
        return ''
    return str(s)

basenameurl = os.path.basename(os.environ['REQUEST_URI'])
index = basenameurl.find('?')
if index != -1:
    basenameurl = basenameurl[:index]

def is_delete_allowed():
    return not "external" in os.environ['REQUEST_URI']

def newreport_rows_to_table(rows):
    REPORTS = ""
    for row in rows:
        REPORTS += '<tr><td>%d</td>' % (row[0])
        REPORTS += '<td><a href="' + basenameurl + '?newid=%d">%s</a></td><td>%s</td>' % (row[0], cgi.escape(row[1]), cgi.escape(xstr(row[2])))
        REPORTS += '<td>%s</td>' % (row[3].replace('\n', '<br>'))
        REPORTS += '<td>' + str(row[4]) + '</td>'
        REPORTS += '<td>' + str(row[5]) + '</td></tr>'

    return REPORTS

def print_new_crashes(dbconn, form):
    template = """<html><head><title>New crash reports</title></head>
<body>
<H2>List of untriaged crash reports (top {COUNT})</H2>
<table border=1>
<tr><th>ID</th><th>Reason</th><th>Crashing Function</th><th>Backtrace</th><th>Reports Count</th><th>Last hit</th></tr>
{REPORTS}
</table>
</body>
</html>
    """

    count = 20
    try:
        if form and form.getfirst("count"):
            count = int(form.getfirst("count"))
    except:
        pass

    REPORTS=""
    try:
        cur = dbconn.cursor()
        cur.execute("SELECT new_crashes.id, new_crashes.reason, new_crashes.func, new_crashes.backtrace, count(triage.newcrash_id) as hitcounts, max(triage.created_at) as last_seen from new_crashes, triage where new_crashes.id = triage.newcrash_id group by new_crashes.id order by last_seen desc, hitcounts desc LIMIT %s", (count,))
        rows = cur.fetchall()
        REPORTS = newreport_rows_to_table(rows)
        cur.close()
    except psycopg2.DatabaseError as e:
        REPORTS = "Database Error"
        print(e)
        pass

    all_items = {'REPORTS': REPORTS, 'BASENAMEURL':basenameurl, 'COUNT':count}

    return template.format(**all_items)

def examine_one_new_crash(dbconn, newid_str):
    if not newid_str.isdigit():
        return "Error! newid must be a number"

    newid = int(newid_str)
    template = """<html><head><title>Edit new crash report</title></head>
<body>
<H2>Editing crashreport #{NEWCRASHID}</H2>
<table border=1>
<form method=\"post\" action=\"{BASENAMEURL}\">
<tr><th>Reason</th><th>Crashing Function</th><th>Where to cut Backtrace</th><th>Reports Count</th></tr>
{REPORT}
</table>
<h2>Added fields:</h2>
<table>
<tr><td>Match messages in logs<br>(every line would be required to be present in log output<br>Copy from \"<b>Messages before crash</b>\" column below):</td><td><textarea name=\"inlogs\" rows=4 cols=50></textarea></td></tr>
<tr><td>Match messages in full crash<br>(every line would be required to be present in crash log output<br>Copy from \"<b>Full Crash</b>\" column below):</td><td><textarea name=\"infullbt\" rows=4 cols=50></textarea></td></tr>
<tr><td>Limit to a test:<br>(Copy from below \"Failing text\"):</td><td><input type=\"text\" name=\"testline\" size=50/></td></tr>
<tr><td>Delete these reports as invalid (real bug in review or some such)</td><td><input type=\"checkbox\" name=\"deletereport\" value=\"yes\"></td></tr>
<tr><td>Bug or comment:</td><td><input type=\"text\" name=\"bugdescription\" size=20 maxLength=20/></td></tr>
<tr><td>Extra info:</td><td><input type=\"text\" name=\"extrainfo\" size=60/></td></tr>
</table>
<input type=\"hidden\" name=\"newid\" value=\"{NEWCRASHID}\"/>
<input type=\"submit\" name=\"newconvert_submit\" value=\"Add to Known bugs\"/>
</form>
<h2>Failures list (last 100):</h2>
<table border=1>
<tr><th>Failing Test</th><th>Full Crash</th><th>Messages before crash</th><th>Comment</th></tr>
{TRIAGE}
</table>
<a href=\"{BASENAMEURL}\">Return to new crashes list</a>
</body>
</html>
    """
    REPORTS = ""
    try:
        cur = dbconn.cursor()
        cur.execute("SELECT new_crashes.id, new_crashes.reason, new_crashes.func, new_crashes.backtrace, count(triage.newcrash_id) as hitcounts from new_crashes, triage where new_crashes.id = triage.newcrash_id and new_crashes.id = %s group by new_crashes.id order by hitcounts desc", [newid])
        if cur.rowcount != 1:
            return "Error! No such element!"
        row = cur.fetchone()
        cur.close()
    except psycopg2.DatabaseError as e:
        print("db error")
        print(e)
        pass
    else:
        REPORTS += '<tr><td>%s</td>' % (cgi.escape(xstr(row[1])))
        REPORTS += '<td>%s</td>' % (cgi.escape(xstr(row[2])))
        REPORTS += '<td>'
        for idx, btline in enumerate(row[3].splitlines()):
            REPORTS += '<input type="radio" name="btline" value="cutat%d"/>%s<br>' % (idx, btline)
        REPORTS += '</td>'
        REPORTS += '<td>' + str(row[4]) + '</td></tr>'

    TRIAGE = ""
    try:
        cur = dbconn.cursor()
        # DISTINCT ON (testline) order by testline
        cur.execute("SELECT id, testline, fullcrash, testlogs, link FROM triage where newcrash_id = %s order by created_at desc LIMIT 100", [newid])
        rows = cur.fetchall()
        cur.close()
        if not rows:
            return "Error! No actual reports for this id!"
    except psycopg2.DatabaseError as e:
        print("db error")
        print(e)
        pass
    else:
        for row in rows:
            linktext = ""
            if "http" in row[4]:
                TRIAGE += '<tr><td><a href="%s">%s</a></td>' % (row[4], cgi.escape(xstr(row[1])))
                linktext = '<a href="%s">Link to test</a>' % (row[4])
            else:
                TRIAGE += '<tr><td>%s</td>' % (cgi.escape(xstr(row[1])))
                linktext = "Externally reported by " + cgi.escape(xstr(row[4]))

            TRIAGE += '<td><div style="overflow: auto; width:30vw; height:300px;">%s</div></td>' % (cgi.escape(row[2]).replace('\n', '<br>'))
            TRIAGE += '<td><div style="overflow: auto; width:50vw; height:300px;">%s</div></td>' % (cgi.escape(xstr(row[3])).replace('\n', '<br>'))
            TRIAGE += "<td>%s</td</tr>" % (linktext)

    all_items = {'NEWCRASHID':newid_str, 'REPORT': REPORTS, 'TRIAGE':TRIAGE, 'BASENAMEURL':basenameurl}

    return template.format(**all_items)

def convert_new_crash(dbconn, form):
    template="""<html><head><title>Converting new crash report</title></head>
<body>
<H2>Converting crashreport #{NEWCRASHID}</H2>
<table border=1>
<form method="post" action=\"#{BASENAMEURL}\">
<h2>Matched {TRACECOUNT} crash traces:</h2>
<tr><th>ID</th><th>Crash Reason</th><th>Crashing Function</th><th>Matched Backtrace</th><th>Matched Reports Count</th><th>Last report time</th></tr>
{REPORTS}
</table>
<textarea name=\"inlogs\" style=\"display:none;\" readonly>{INLOGS}</textarea>
<textarea name=\"infullbt\" style=\"display:none;\" readonly>{INFULLBT}</textarea>
<input type=\"hidden\" name=\"testline\" value=\"{TESTLINE}\"/>
<input type=\"hidden\" name=\"bugdescription\" value=\"{BUGDESCRIPTION}\"/>
<input type=\"hidden\" name=\"extrainfo\" value=\"{EXTRAINFO}\"/>
<input type=\"hidden\" name=\"newid\" value=\"{NEWCRASHID}\"/>
<input type=\"hidden\" name=\"btline\" value=\"{BTLINE}\"/>
<input type=\"hidden\" name=\"deletereport\" value=\"{DELETEREPORT}\"/>
<input type=\"hidden\" name=\"confirm\" value=\"yes\"/>
<input type=\"submit\" name=\"newconvert_submit\" value=\"Confirm Adding to Known bugs\"/>
</form>
<p>
<a href=\"{BASENAMEURL}?newid={NEWCRASHID}\">Return to view ID {NEWCRASHID}</a> | <a href=\"{BASENAMEURL}\">Return to new crashes list</a>
</body>
</html>
    """
    newid_str = form.getfirst("newid")
    if not newid_str or not newid_str.isdigit():
        return "Error, not numeric id"
    newid = int(newid_str)
    testline = form.getfirst("testline", "")
    bugdescription = form.getfirst("bugdescription", "")
    extrainfo = form.getfirst("extrainfo", "")
    inlogs = form.getfirst("inlogs", "")
    infullbt = form.getfirst("infullbt", "")
    confirm = form.getfirst("confirm", "")
    btline_str = form.getfirst("btline", "")
    deletereport = form.getfirst("deletereport", "")

    if not bugdescription and deletereport != "yes":
        return "Error! Bug description cannot be empty and not deleting"
    if bugdescription and deletereport == "yes":
        return "Error! Cannot assign bug numbers to reports you are deleting"
    # Get backtrace info
    try:
        cur = dbconn.cursor()
        cur.execute("SELECT backtrace, func, reason FROM new_crashes WHERE id=%s", [ newid ])
        if cur.rowcount == 0:
            return "No such id!"
        row = cur.fetchone()
        backtrace = row[0].splitlines()
        func = xstr(row[1])
        reason = row[2]
        cur.execute("SELECT count(*) FROM triage WHERE newcrash_id=%s", [ newid ])
        triagereports = cur.fetchone()[0]
        cur.close()
    except psycopg2.DatabaseError:
        return "Db error"

    if not btline_str:
        btlines = len(backtrace)
    else:
        tmp = btline_str.replace("cutat", "")
        if not tmp.isdigit():
            return "Wrong bt cutat value"
        btlines = int(tmp) + 1
    if btlines > len(backtrace) + 1 or btlines < 2:
        if btlines != len(backtrace): # for small backtraces it's ok
            return "Cannot cut too low or too high"

    backtrace = backtrace[:btlines]

    # Now see how many matches we have
    btline = '\n'.join(backtrace)
    SELECTline = "SELECT new_crashes.id, new_crashes.reason, new_crashes.func, new_crashes.backtrace, count(triage.newcrash_id) as hitcount, max(triage.created_at) as last_seen FROM new_crashes, triage WHERE triage.newcrash_id=new_crashes.id AND new_crashes.reason=%s AND strpos(new_crashes.backtrace, %s) > 0"
    SELECTvars = [ reason, btline ]
    EXTRACONDS = ""
    EXTRACONDvars = []
    if func:
        EXTRACONDS += " AND new_crashes.func=%s"
        EXTRACONDvars.append(func)
    if testline:
        EXTRACONDS += " AND strpos(triage.testline, %s) > 0"
        EXTRACONDvars.append(testline)
    if inlogs:
        inlogs_lines = []
        for line in inlogs.splitlines():
            line = line.strip()
            EXTRACONDS += " AND strpos(triage.testlogs, %s) > 0"
            EXTRACONDvars.append(line)
            inlogs_lines.append(line)
            inlogs_cleaned = '\n'.join(inlogs_lines)
    else:
        inlogs_cleaned = ""

    if infullbt:
        infullbt_lines = []
        for line in infullbt.splitlines():
            line = line.strip()
            EXTRACONDS += " AND strpos(triage.fullcrash, %s) > 0"
            EXTRACONDvars.append(line)
            infullbt_lines.append(line)
            infullbt_cleaned = '\n'.join(infullbt_lines)
    else:
        infullbt_cleaned = ""

    SELECTline += EXTRACONDS
    SELECTline += " group by new_crashes.id order by last_seen desc, hitcount desc"
    SELECTvars += EXTRACONDvars

    REPORTS = ""
    try:
        cur = dbconn.cursor()
        cur.execute(SELECTline, SELECTvars)
        if cur.rowcount == 0:
            return "Cannot find anything matching: " + SELECTline + " " + str(SELECTvars)
        TRACECOUNT = cur.rowcount
        rows = cur.fetchall()
        cur.close()
        REPORTS = newreport_rows_to_table(rows)

    except psycopg2.DatabaseError as e:
        REPORTS = "DB Error " + str(e)
        TRACECOUNT = 0

    if confirm != "yes":
        all_items = {'NEWCRASHID':newid_str, 'REPORTS': REPORTS, 'TRACECOUNT':TRACECOUNT, 'INLOGS':inlogs_cleaned, 'BUGDESCRIPTION':bugdescription, 'EXTRAINFO':extrainfo, 'TESTLINE':testline, 'INFULLBT':infullbt_cleaned, 'BTLINE':btline_str, 'DELETEREPORT':deletereport, 'BASENAMEURL':basenameurl}
        return template.format(**all_items)
    elif not is_delete_allowed():
        return "Actual deleting on external scripts is disabled"

    template = """<html><head><title>Converting new crash report</title></head>
<body>
<H2>Converting crashreport #{NEWCRASHID}</H2>
{MALOOREPORT}
<a href=\"{BASENAMEURL}\">Return to new crashes list</a>
</body>
</html>
"""
    # Assemble array of newbug IDs affected in a form that postgres understands (1, 2,3, ...)
    ids = []
    for row in rows:
        ids.append(str(row[0]))
    NEWIDS = '(' + ', '.join(ids) + ')'

    malooreport = ""
    # This was our second pass, we now need to insert the data into known crashes
    # Or if it was a delete request, don't create anything, just delete
    if deletereport != 'yes':
        if not add_known_crash(testline, reason, func, btline, inlogs_cleaned, infullbt_cleaned, bugdescription, extrainfo, DBCONN=dbconn):
            return "Failed to add new known crash"

        malooreport += "<h2>Maloo update report</h2>"
        malooreport += "<table border=1><tr><th>maloo link</th><th>Update result</th></tr>"
        # Now we need to gather all links and post the vetter result to maloo:
        try:
            reporter = mymaloo_bugreporter.maloo_poster()
            cur = dbconn.cursor()
            cur.execute('SELECT triage.link, triage.testline FROM triage, new_crashes WHERE newcrash_id in ' + NEWIDS + EXTRACONDS, EXTRACONDvars)
            rows = cur.fetchall()
            cur.close()
            for row in rows:
                link = row[0]
                # we could have excluded it with select, but that's probably
                # not all that important with small numbers we have here
                # and I need to do extra hoops to save old values and stuff
                # in EXTRACONDS and EXTRACONDvars
                if link.startswith('https://testing.whamcloud.com'):
                    res = reporter.associate_bug_by_url(link, bugdescription, row[1])
                    malooreport += '<tr><td><a href="%s">%s</a></td><td>' % (link, link)
                    if res:
                        malooreport += "Success"
                    else:
                        malooreport += "Error: " + reporter.error
                    malooreport += '</td></tr>'
        except psycopg2.DatabaseError as e:
            malooreport += "DB Error " + str(e)
            print(str(e))

    malooreport += "</table>"

    # and remove all matching reports.
    try:
        cur = dbconn.cursor()
        cur.execute('DELETE FROM triage USING new_crashes WHERE newcrash_id in ' + NEWIDS + EXTRACONDS, EXTRACONDvars)
        dbconn.commit()

        # Now we need to see if any new crashes have zero triage reports left
        # and nuke those
        cur.execute('DELETE FROM new_crashes WHERE id in ' + NEWIDS + ' AND NOT EXISTS (SELECT 1 FROM triage WHERE triage.newcrash_id=new_crashes.id)')
        dbconn.commit()
        cur.close()
    except psycopg2.DatabaseError as e:
        return "DB Error on delete " + str(e)

    all_items = {'NEWCRASHID':', '.join(ids), 'BASENAMEURL':basenameurl, 'MALOOREPORT':malooreport}
    return template.format(**all_items)


if __name__ == "__main__":
    form = cgi.FieldStorage()
    if not form or form.getfirst("count"):
        result = print_new_crashes(dbconn, form)
    elif form.getfirst("newconvert_submit"):
        result = convert_new_crash(dbconn, form)
    elif form.getfirst("newid"):
        result = examine_one_new_crash(dbconn, form.getfirst("newid"))


    print(result)
dbconn.close()
