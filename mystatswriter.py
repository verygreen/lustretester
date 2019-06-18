import os
import time
from influxdb import InfluxDBClient

a_influxhost = 'localhost'              #InfluxDB Host
a_influxport = 8086                     #InfluxDB Port
a_influxuser = '' #'tesla'                  #InfluxDB Username
a_influxpass = '' #'<influxdbpassword>'     #InfluxDB Password
a_influxdb = 'testerstate'                    #InfluxDB Datebasename

class StatsWriter:
    def __init__(self):
        self.influxclient = InfluxDBClient(a_influxhost, a_influxport,
                                           a_influxuser, a_influxpass,
                                           a_influxdb)

    def builderstats(self, total, dead, busy, idle, queue):
        json_body = [
            {
                "measurement": "builder",
                "tags": {
                    "host": os.environ['HOSTNAME'],
                    "metric": "testerstats"
                },
                "time": int(time.time() * 1000000000),
                "fields": {
                   "queue" : queue,
                   "total" : total,
                   "busy" : busy,
                   "dead" : dead,
                   "idle" : idle,
                }
            }
        ]
        self.influxclient.write_points(json_body)

    def testerstats(self, total, dead, busy, invalid, idle, queue):
        json_body = [
            {
                "measurement": "tester",
                "tags": {
                    "host": os.environ['HOSTNAME'],
                    "metric": "testerstats"
                },
                "time": int(time.time() * 1000000000),
                "fields": {
                   "queue" : queue,
                   "total" : total,
                   "busy" : busy,
                   "invalid" : invalid,
                   "dead" : dead,
                   "idle" : idle,
                }
            }
        ]
        self.influxclient.write_points(json_body)

    def mainstats(self, items):
        json_body = [
            {
                "measurement": "main",
                "tags": {
                    "host": os.environ['HOSTNAME'],
                    "metric": "testerstats"
                },
                "time": int(time.time() * 1000000000),
                "fields": {
                   "items" : items,
                }
            }
        ]
        self.influxclient.write_points(json_body)
