#
# CDR-Stats License
# http://www.cdr-stats.org
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2011-2012 Star2Billing S.L.
#
# The Initial Developer of the Original Code is
# Arezqui Belaid <info@star2billing.com>
#
from django.conf import settings
from django.utils.translation import gettext as _
import MySQLdb as Database

from django.core.exceptions import ObjectDoesNotExist
from django.utils.safestring import mark_safe

from pymongo.connection import Connection
from pymongo.errors import ConnectionFailure

from cdr.models import Switch, HangupCause
from cdr.functions_def import *
from cdr_alert.functions_blacklist import *
from country_dialcode.models import Prefix
from random import choice
from uuid import uuid1
from datetime import *
import calendar
import time
import sys
import random
import json, ast
import re

random.seed()

HANGUP_CAUSE = ['NORMAL_CLEARING','NORMAL_CLEARING','NORMAL_CLEARING','NORMAL_CLEARING',
                'USER_BUSY', 'NO_ANSWER', 'CALL_REJECTED', 'INVALID_NUMBER_FORMAT']

CDR_TYPE = {"freeswitch":1, "asterisk":2, "yate":3, "opensips":4, "kamailio":5}

#value 0 per default, 1 in process of import, 2 imported successfully and verified
STATUS_SYNC = {"new":0, "in_process": 1, "verified":2}

DISPOSITION_TRANSLATION = {
    0: 0,
    1: 16,  #ANSWER
    2: 17,  #BUSY
    3: 19,  #NOANSWER
    4: 21,  #CANCEL
    5: 34,  #CONGESTION
    6: 47,  #CHANUNAVAIL
    7: 0,   #DONTCALL
    8: 0,   #TORTURE
    9: 0,   #INVALIDARGS
}

# Assign collection names to variables
CDR_COMMON = settings.DB_CONNECTION[settings.CDR_MONGO_CDR_COMMON]
CDR_MONTHLY = settings.DB_CONNECTION[settings.CDR_MONGO_CDR_MONTHLY]
CDR_DAILY = settings.DB_CONNECTION[settings.CDR_MONGO_CDR_DAILY]
CDR_HOURLY = settings.DB_CONNECTION[settings.CDR_MONGO_CDR_HOURLY]
CDR_COUNTRY_REPORT = settings.DB_CONNECTION[settings.CDR_MONGO_CDR_COUNTRY_REPORT]



def print_shell(shell, message):
    if shell:
        print message


def import_cdr_asterisk_mysql(shell=False):
    #TODO : dont use the args here
    # Browse settings.ASTERISK_CDR_MYSQL_IMPORT and for each IP check if the IP exist in our Switch objects
    # If it does we will connect to that Database and import the data as we do below

    print_shell(shell, "Starting the synchronization...")

    if settings.LOCAL_SWITCH_TYPE != 'asterisk':
        print_shell(shell, "The switch is not configured to import Asterisk...")
        return False

    #loop within the Mongo CDR Import List
    for ipaddress in settings.ASTERISK_CDR_MYSQL_IMPORT:
        #Select the Switch ID
        print_shell(shell, "Switch : %s" % ipaddress)

        DEV_ADD_IP = False
        #uncomment this if you need to import from a fake different IP / used for dev
        #DEV_ADD_IP = '127.0.0.2'

        if DEV_ADD_IP:
            previous_ip = ipaddress
            ipaddress = DEV_ADD_IP
        try:
            switch = Switch.objects.get(ipaddress=ipaddress)
        except Switch.DoesNotExist:
            switch = Switch(name=ipaddress, ipaddress=ipaddress)
            switch.save()

        if not switch.id:
            print "Error when adding new Switch!"
            raise SystemExit

        if DEV_ADD_IP:
            ipaddress = previous_ip

        #Connect on Mysql Database
        db_name = settings.ASTERISK_CDR_MYSQL_IMPORT[ipaddress]['db_name']
        table_name = settings.ASTERISK_CDR_MYSQL_IMPORT[ipaddress]['table_name']
        user = settings.ASTERISK_CDR_MYSQL_IMPORT[ipaddress]['user']
        password = settings.ASTERISK_CDR_MYSQL_IMPORT[ipaddress]['password']
        host = settings.ASTERISK_CDR_MYSQL_IMPORT[ipaddress]['host']
        try:
            connection = Database.connect(user=user, passwd=password, db=db_name, host=host)
            cursor = connection.cursor()
        except ConnectionFailure, e:
            sys.stderr.write("Could not connect to Mysql: %s - %s" % \
                                                            (e, ipaddress))
            sys.exit(1)


        try:
            cursor.execute ("SELECT VERSION() from %s WHERE import_cdr IS NOT NULL LIMIT 0,1" % table_name)
            row = cursor.fetchone()
        except:
            #Add missing field to flag import
            cursor.execute("ALTER TABLE %s  ADD import_cdr TINYINT NOT NULL DEFAULT '0'" % table_name)
            cursor.execute ("ALTER TABLE %s ADD INDEX (import_cdr)" % table_name)

        #cursor.execute ("SELECT count(*) FROM %s WHERE import_cdr=0" % table_name)
        #row = cursor.fetchone()
        #total_record = row[0]

        #print total_loop_count
        count_import = 0

        cursor.execute ("SELECT dst, UNIX_TIMESTAMP(calldate), clid, channel, duration, billsec, disposition, accountcode, uniqueid FROM %s WHERE import_cdr=0" % table_name)
        row = cursor.fetchone()
        while row is not None:
            print ", ".join([str(c) for c in row])

            callerid = row[2]
            try:
                m = re.search('"(.+?)" <(.+?)>', callerid)
                callerid_name = m.group(1)
                callerid_number = m.group(2)
            except:
                callerid_name = ''
                callerid_number = callerid

            channel = row[3]
            duration = int(row[4])
            billsec = int(row[5])
            ast_disposition = int(row[6])
            try:
                transdisposition = DISPOSITION_TRANSLATION[ast_disposition]
            except:
                transdisposition = 0
            hangup_cause_id = get_hangupcause_id(transdisposition)

            accountcode = int(row[7])
            uniqueid = row[8]
            start_uepoch = datetime.fromtimestamp(int(row[1]))
            answer_uepoch = start_uepoch
            end_uepoch = datetime.fromtimestamp(int(row[1]) + int(duration)) 

            # Check Destination number
            destination_number = row[0]

            #country_id = 198 # spain default
            # number startswith 0 or `+` sign

            #remove leading +
            sanitized_destination = re.sub("^\++","",destination_number)
            #remove leading 011
            sanitized_destination = re.sub("^011+","",sanitized_destination)
            #remove leading 00
            sanitized_destination = re.sub("^0+","",sanitized_destination)
            
            prefix_list = prefix_list_string(sanitized_destination)

            authorized = 1 # default
            #check desti against whiltelist
            authorized = chk_prefix_in_whitelist(prefix_list)
            if authorized:
                authorized = 1 # allowed destination
            else:
                #check desti against blacklist
                authorized = chk_prefix_in_blacklist(prefix_list)
                if not authorized:
                    authorized = 0 # not allowed destination

            country_id = get_country_id(prefix_list)

            if get_country_id==0:
                #TODO: Add logger
                print "Error to find the country_id %s" % destination_number

            # Prepare global CDR
            cdr_record = {
                'switch_id': switch.id,
                'caller_id_number': callerid_number,
                'caller_id_name': callerid_name,
                'destination_number': destination_number,
                'duration': duration,
                'billsec': billsec,
                'hangup_cause_id': hangup_cause_id,
                'accountcode': accountcode,
                'direction': "inbound",
                'uuid': uniqueid,
                'remote_media_ip': '',
                'start_uepoch': start_uepoch,
                'answer_uepoch': answer_uepoch,
                'end_uepoch': end_uepoch,
                'mduration': '',
                'billmsec': '',
                'read_codec': '',
                'write_codec': '',
                'cdr_type': CDR_TYPE["asterisk"],
                'cdr_object_id': uniqueid,
                'country_id': country_id,
                'authorized': authorized,
            }

            # record global CDR
            CDR_COMMON.insert(cdr_record)

            print_shell(shell, "Sync CDR (cid:%s, dest:%s, dur:%s, hg:%s, country:%s, auth:%s)" % (
                                        callerid_number,
                                        destination_number,
                                        duration,
                                        hangup_cause_id,
                                        country_id,
                                        authorized,))
            count_import = count_import + 1

            # change import_cdr flag
            #update_cdr_collection(importcdr_handler, cdr['_id'], 'import_cdr')
            
            # Store monthly cdr collection with unique import
            current_y_m = datetime.strptime(str(start_uepoch)[:7], "%Y-%m")
            CDR_MONTHLY.update(
                        {
                            'start_uepoch': current_y_m,
                            'destination_number': destination_number,
                            'hangup_cause_id': hangup_cause_id,
                            'accountcode': accountcode,
                            'switch_id': switch.id,
                        },
                        {
                            '$inc':
                                {'calls': 1,
                                 'duration': duration }
                        }, upsert=True)

            # Store daily cdr collection with unique import
            current_y_m_d = datetime.strptime(str(start_uepoch)[:10], "%Y-%m-%d")
            CDR_DAILY.update(
                    {
                        'start_uepoch': current_y_m_d,
                        'destination_number': destination_number,
                        'hangup_cause_id': hangup_cause_id,
                        'accountcode': accountcode,
                        'switch_id': switch.id,
                    },
                    {
                        '$inc':
                            {'calls': 1,
                             'duration': duration }
                    },upsert=True)

            # Store hourly cdr collection with unique import
            current_y_m_d_h = datetime.strptime(str(start_uepoch)[:13], "%Y-%m-%d %H")
            CDR_HOURLY.update(
                        {
                            'start_uepoch': current_y_m_d_h,
                            'destination_number': destination_number,
                            'hangup_cause_id': hangup_cause_id,
                            'accountcode': accountcode,
                            'switch_id': switch.id,},
                        {
                            '$inc': {'calls': 1,
                                     'duration': duration }
                        },upsert=True)

            # Country report collection
            current_y_m_d_h_m = datetime.strptime(str(start_uepoch)[:16], "%Y-%m-%d %H:%M")
            CDR_COUNTRY_REPORT.update(
                                {
                                    'start_uepoch': current_y_m_d_h_m,
                                    'country_id': country_id,
                                    'accountcode': accountcode,
                                    'switch_id': switch.id,},
                                {
                                    '$inc': {'calls': 1,
                                             'duration': duration }
                                },upsert=True)

            # Flag the CDR as imported
            #importcdr_handler.update(
            #            {'_id': cdr['_id']},
            #            {'$set': {'import_cdr': 1, 'import_cdr_monthly': 1, 'import_cdr_daily': 1, 'import_cdr_hourly': 1}}
            #)


            print "Record inserted..."
            sys.exit(1)

            #Fetch a other record
            row = cursor.fetchone()
        
        cursor.close()
        connection.close()

        if count_import > 0:
            # Apply index
            CDR_COMMON.ensure_index([("start_uepoch", -1)])
            CDR_MONTHLY.ensure_index([("start_uepoch", -1)])
            CDR_DAILY.ensure_index([("start_uepoch", -1)])
            CDR_HOURLY.ensure_index([("start_uepoch", -1)])
            CDR_COUNTRY_REPORT.ensure_index([("start_uepoch", -1)])

        print_shell(shell, "Import on Switch(%s) - Record(s) imported:%d" % (ipaddress, count_import))