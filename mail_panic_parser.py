import os
import sys
from pprint import pprint
import mailbox
import psycopg2

from mycrashanalyzer import extract_crash_from_dmesg, is_known_crash, add_new_crash


if __name__ == "__main__":

    mbox = mailbox.mbox(sys.argv[1])

    dbconn = psycopg2.connect(dbname="crashinfo", user="crashinfo", password="blah", host="localhost")

    for key in mbox.keys():
        crashfile = mbox.get_file(key)
        (lasttest, entirecrash, lasttestlogs, crashtrigger, function, abbreviatedbt) = extract_crash_from_dmesg(crashfile)
        crashfile.close()
        pprint(lasttest)
        #pprint(crashtrigger)
        #pprint(function)
        #pprint(abbreviatedbt)
        #pprint(lasttestlogs)
        if not abbreviatedbt and not "Inexact backtrace" in entirecrash:
            print("cannot find bt")
            pprint(entirecrash)
            continue
        (result, extrainfo) = is_known_crash(lasttest, crashtrigger, function, abbreviatedbt, entirecrash, lasttestlogs, DBCONN=dbconn)
        #pprint(result)
        if not result:
            # Need to grab extra info
            message = mbox.get_message(key)
            from_addr = message.get("From").replace('<','').replace('>', '')
            source = None
            if "crash-report@hisoka.home.linuxhacker.ru" == from_addr:
                source = "From green boilpot email"
            elif "crash-report@whamcloud.com" == from_addr:
                source = "onyx-68 boilpot email"
            elif "noreply@maloo-prod.onyx.whamcloud.com" == from_addr:
                if not message.is_multipart():
                    for line in message.get_payload().splitlines():
                        if line.startswith('The following test session crashed:'):
                            source = line.strip().replace('The following test session crashed: ','')
                            break
            if not source:
                source = "Unrecognized email message from " + from_addr

            # add it to triage
            (newid, numreports) = add_new_crash(lasttest, crashtrigger, function, abbreviatedbt, entirecrash, lasttestlogs, source)
            # 0 id means error
            if not newid:
                print("Cannot store new crash info!")
        else:
            print("Got a match, it's " + result + " extrainfo " + extrainfo)


    dbconn.close()
