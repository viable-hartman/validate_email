# RFC 2822 - style email validation for Python
# (c) 2012 Syrus Akbary <me@syrusakbary.com>
# Extended from (c) 2011 Noel Bush <noel@aitools.org>
# for support of mx and user check
# This code is made available to you under the GNU LGPL v3.
#
# This module provides a single method, valid_email_address(),
# which returns True or False to indicate whether a given address
# is valid according to the 'addr-spec' part of the specification
# given in RFC 2822.  Ideally, we would like to find this
# in some other library, already thoroughly tested and well-
# maintained.  The standard Python library email.utils
# contains a parse_addr() function, but it is not sufficient
# to detect many malformed addresses.
#
# This implementation aims to be faithful to the RFC, with the
# exception of a circular definition (see comments below), and
# with the omission of the pattern components marked as "obsolete".

import logging
import pprint
import os
import sys
import re
import smtplib
import socket

# sqlite3 imports for looking up known MX servers.
try:
    import sqlite3
except (ImportError, AttributeError):
    pass


    class ServerError(Exception):
        pass

# All we are really doing is comparing the input string to one
# gigantic regular expression.  But building that regexp, and
# ensuring its correctness, is made much easier by assembling it
# from the "tokens" defined by the RFC.  Each of these tokens is
# tested in the accompanying unit test file.
#
# The section of RFC 2822 from which each pattern component is
# derived is given in an accompanying comment.
#
# (To make things simple, every string below is given as 'raw',
# even when it's not strictly necessary.  This way we don't forget
# when it is necessary.)
#
WSP = r'[\s]'  # see 2.2.2. Structured Header Field Bodies
CRLF = r'(?:\r\n)'  # see 2.2.3. Long Header Fields
NO_WS_CTL = r'\x01-\x08\x0b\x0c\x0f-\x1f\x7f'  # see 3.2.1. Primitive Tokens
QUOTED_PAIR = r'(?:\\.)'  # see 3.2.2. Quoted characters
FWS = r'(?:(?:' + WSP + r'*' + CRLF + r')?' + WSP + r'+)'  # see 3.2.3. Folding white space and comments
CTEXT = r'[' + NO_WS_CTL + r'\x21-\x27\x2a-\x5b\x5d-\x7e]'  # see 3.2.3
# (NB: The RFC includes COMMENT here as well, but that would be circular.)
CCONTENT = r'(?:' + CTEXT + r'|' + QUOTED_PAIR + r')'  # see 3.2.3
COMMENT = r'\((?:' + FWS + r'?' + CCONTENT + r')*' + FWS + r'?\)'  # see 3.2.3
CFWS = r'(?:' + FWS + r'?' + COMMENT + ')*(?:' + FWS + '?' + COMMENT + '|' + FWS + ')'  # see 3.2.3
ATEXT = r'[\w!#$%&\'\*\+\-/=\?\^`\{\|\}~]'  # see 3.2.4. Atom
ATOM = CFWS + r'?' + ATEXT + r'+' + CFWS + r'?'  # see 3.2.4
DOT_ATOM_TEXT = ATEXT + r'+(?:\.' + ATEXT + r'+)*'  # see 3.2.4
DOT_ATOM = CFWS + r'?' + DOT_ATOM_TEXT + CFWS + r'?'  # see 3.2.4
QTEXT = r'[' + NO_WS_CTL + r'\x21\x23-\x5b\x5d-\x7e]'  # see 3.2.5. Quoted strings
QCONTENT = r'(?:' + QTEXT + r'|' + QUOTED_PAIR + r')'  # see 3.2.5
QUOTED_STRING = CFWS + r'?' + r'"(?:' + FWS + r'?' + QCONTENT + r')*' + FWS + r'?' + r'"' + CFWS + r'?'
LOCAL_PART = r'(?:' + DOT_ATOM + r'|' + QUOTED_STRING + r')'  # see 3.4.1. Addr-spec specification
DTEXT = r'[' + NO_WS_CTL + r'\x21-\x5a\x5e-\x7e]'  # see 3.4.1
DCONTENT = r'(?:' + DTEXT + r'|' + QUOTED_PAIR + r')'  # see 3.4.1
DOMAIN_LITERAL = CFWS + r'?' + r'\[' + r'(?:' + FWS + r'?' + DCONTENT + r')*' + FWS + r'?\]' + CFWS + r'?'  # see 3.4.1
DOMAIN = r'(?:' + DOT_ATOM + r'|' + DOMAIN_LITERAL + r')'  # see 3.4.1
ADDR_SPEC = LOCAL_PART + r'@' + DOMAIN  # see 3.4.1
VALID_ADDRESS_REGEXP = '^' + ADDR_SPEC + '$'  # A valid address will match exactly the 3.4.1 addr-spec.
MX_DNS_CACHE = {}
MX_CHECK_CACHE = {}

# Set up the global logger to stdout
logger = logging.getLogger(__name__)
logger.setLevel(logging.CRITICAL)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.CRITICAL)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)


def is_disposable(email):
    """Indicate whether the email is known as being a disposable email or not"""
    email_domain = email.rsplit('@', 1)
    if email_domain in _disposable:
        logger.warn("Email %s is flagged as disposable (domain=%s)", email, domain)
        return True
    return False


def get_known_domain(hostname, sql_conn=None, decrypt=None):
    # If sql_conn defined first check if this is a known domain we have options for.
    if sql_conn:
        c = sql_conn.cursor()
        logger.debug(u"Selecting from view")
        c.execute('SELECT * FROM connectionView WHERE domain = ?', [(hostname)])
        data = c.fetchone()
        logger.debug(u"SQL DATA: %s", pprint.pformat(data, indent=4))
        if data:
            # Decrypt username and password data if it exists.
            username = data[2]
            password = data[3]
            if data[2] is not None:
                if decrypt is not None:
                    username = decrypt(data[2])
                    logger.debug(u"Looked up username: %s", username)
            if data[3] is not None:
                if decrypt is not None:
                    password = decrypt(data[3])
            return {data[1]: {"domain": data[0], "username": username, "password": password, "is_ssl": data[4], "port": data[5]},}
    logger.debug(u"RETURNING NONE")
    return None


def get_mx_ip(hostname, sql_conn=None, decrypt=None):
    logger.debug(u"Looking for MX Records for %s", hostname)
    known_domain = get_known_domain(hostname, sql_conn, decrypt)
    if known_domain:
        logger.debug(u"Results of first lookup: %s", pprint.pformat(known_doamin, indent=4))
        return known_domain
  
    # Import dnspython 
    from dns import resolver, exception
    # Perform DNS lookup with dnspython if this isn't already in cache.
    if hostname not in MX_DNS_CACHE:
        try:
            logger.debug(u"  ~~~~ get_mx_ip hostname not in MX_DNS_CACHE!!!")
            # Store the DNS cache entry with same options as sql_conn cached item.
            cache_item = {}
            for mx in resolver.query(hostname, 'MX'):
                server = mx.exchange.to_text(omit_final_dot=True)
                logger.debug(u"  ~~~~ get_mx_ip checking server %s!!!", server)
                # Check if this domain maps to a known top level domain
                topleveldomain = '.'.join(server.split('.')[-2:])
                logger.debug(u"  ~~~~ get_mx_ip topleveldomain %s!!!", topleveldomain)
                known_domain = get_known_domain(topleveldomain, sql_conn, decrypt)
                if known_domain:
                    logger.debug(u"  ~~~~ get_mx_ip known_domain %s!!!", known_domain)
                    return known_domain
                # TODO: create way to discover if is_ssl (maybe check port(s) 465 and 587)
                cache_item[server] = {"domain": hostname, "username": None, "password": None, "is_ssl": 0, "port": 25}
            MX_DNS_CACHE[hostname] = cache_item
        except exception.Timeout as e:
            return False
        except exception.DNSException as e:
            if isinstance(e, resolver.NXDOMAIN):  # or e.rcode == 2:  # SERVFAIL
                MX_DNS_CACHE[hostname] = None
            else:
                raise e

    logger.debug(u"  ~~~~ LOOKED UP %s!!!", MX_DNS_CACHE[hostname])
    return MX_DNS_CACHE[hostname]


def check_command(result_tuple, server_name='server', ok_codes=[250], fail_codes=[550]):
    status, mes = result_tuple
    if status in fail_codes:
        logger.debug(u'%s in fail codes, answer: %s - %s', server_name, status, mes)
        return False
    if status in ok_codes:
        logger.debug(u'%s in success codes, answer: %s - %s', server_name, status, mes)
        return True
    return None


def check_command_for_server(server_name):
    def wrapper(*args, **kwargs):
        kwargs['server_name'] = server_name
        return check_command(*args, **kwargs)
    return wrapper


def validate_email(email,
                   check_mx=False,
                   verify=False,
                   debug=False,
                   smtp_timeout=5,
                   allow_disposable=True,
                   sending_email=None,
                   sql_conn=None,
                   decrypt=None,
                   ):
    """Indicate whether the given string is a valid email address
    according to the 'addr-spec' portion of RFC 2822 (see section
    3.4.1).  Parts of the spec that are marked obsolete are *not*
    included in this test, and certain arcane constructions that
    depend on circular definitions in the spec may not pass, but in
    general this should correctly identify any email address likely
    to be in use as of 2011."""
    if debug:
        logger.setLevel(logging.DEBUG)
        ch.setLevel(logging.DEBUG)

    try:
        assert re.match(VALID_ADDRESS_REGEXP, email) is not None
        check_mx |= verify
        if not allow_disposable and is_disposable(email):
            return False

        if check_mx:
            hostname = email[email.find('@') + 1:]
            mx_hosts = get_mx_ip(hostname, sql_conn, decrypt)
            logger.debug(pprint.pformat(mx_hosts, indent=4))
            if mx_hosts is None:     # Implies DNS couldn't find MX records
                return False
            elif mx_hosts is False:  # Implies DNS timed out or failed.
                return None
            for mx in mx_hosts:
                try:
                    check = check_command_for_server(mx)
                    if not verify and mx in MX_CHECK_CACHE:
                        logger.debug(u"    ~~~ Returning from cache: %s", MX_CHECK_CACHE[mx])
                        return MX_CHECK_CACHE[mx]
                    
                    if mx_hosts[mx]['is_ssl'] > 0:
                        smtp = smtplib.SMTP_SSL(timeout=smtp_timeout)
                        logger.debug(u"    ~~~ Connecting to: %s:%s over SSL socket", mx, mx_hosts[mx]['port'])
                    else:
                        smtp = smtplib.SMTP(timeout=smtp_timeout)
                        logger.debug(u"    ~~~ Connecting to: %s:%s over standard socket", mx, mx_hosts[mx]['port'])

                    smtp.connect(host=mx, port=mx_hosts[mx]['port'])

                    if mx_hosts[mx]['username'] and mx_hosts[mx]['password']:  # Login is required.
                        logger.debug(u"    ~~~ Logging Into: %s with user %s", mx, mx_hosts[mx]['username'])
                        smtp.login(mx_hosts[mx]['username'], mx_hosts[mx]['password'])

                    MX_CHECK_CACHE[mx] = True

                    logger.debug(u"    ~~~ MX_CHECK_CACHE: %s VAL: %s", mx, MX_CHECK_CACHE[mx])
                    if not verify:
                        return True
                    
                    if not check(smtp.helo()):
                        continue 

                    # Properly set the mail from address.                    
                    if mx_hosts[mx]['username']:
                        sending_email = mx_hosts[mx]['username']
                    elif not sending_email:
                        sending_email = 'admin@%s' % (hostname)

                    if not check(smtp.mail(sending_email)):
                        continue
                    
                    # Checking RCPT
                    rcpt = check(smtp.rcpt(email))
                    if rcpt:
                        return True
                    elif rcpt is None:
                        continue
                    else:
                        return False  # Implies 550 on rcpt was given.
                except smtplib.SMTPServerDisconnected as ssd:  # Server not permits verify user
                    logger.debug(u'%s disconected.', mx)
                except smtplib.SMTPConnectError as sce:
                    logger.debug(u'Unable to connect to %s.', mx)
                finally:
                    try:
                        smtp.quit()
                    except smtplib.SMTPServerDisconnected:
                        pass
 
            return None  # May want to return false here.
    except AssertionError:
        return False
    except socket.error as e:
        logger.debug('socket.error exception raised (%s).', e)
        return None
    #except Exception as e:  # Removing catch all so I can catch unknown error in service code.
    #    logger.debug('Unknown exception raised (%s).', e)
    #    return False

    return True


_disposable = ["0-mail.com", "027168.com", "0815.ru", "0815.ry", "0815.su", "0845.ru", "0clickemail.com", "0wnd.net",
               "0wnd.org", "0x207.info", "1-8.biz", "100likers.com", "10mail.com", "10mail.org", "10minut.com.pl",
               "10minutemail.cf", "10minutemail.co.uk", "10minutemail.co.za", "10minutemail.com", "10minutemail.de",
               "10minutemail.ga", "10minutemail.gq", "10minutemail.ml", "10minutemail.net", "10minutesmail.com",
               "10x9.com", "123-m.com", "12houremail.com", "12minutemail.com", "12minutemail.net", "140unichars.com",
               "147.cl", "14n.co.uk", "1ce.us", "1chuan.com", "1fsdfdsfsdf.tk", "1mail.ml", "1pad.de", "1st-forms.com",
               "1to1mail.org", "1zhuan.com", "20email.eu", "20email.it", "20mail.in", "20mail.it", "20minutemail.com",
               "2120001.net", "21cn.com", "24hourmail.com", "24hourmail.net", "2fdgdfgdfgdf.tk", "2prong.com",
               "30minutemail.com", "33mail.com", "36ru.com", "3d-painting.com", "3l6.com", "3mail.ga",
               "3trtretgfrfe.tk", "4-n.us", "418.dk", "42o.org", "4gfdsgfdgfd.tk", "4mail.cf", "4mail.ga",
               "4warding.com", "4warding.net", "4warding.org", "5ghgfhfghfgh.tk", "5gramos.com", "5mail.cf", "5mail.ga",
               "5oz.ru", "5x25.com", "60minutemail.com", "672643.net", "675hosting.com", "675hosting.net",
               "675hosting.org", "6hjgjhgkilkj.tk", "6ip.us", "6mail.cf", "6mail.ga", "6mail.ml", "6paq.com",
               "6url.com", "75hosting.com", "75hosting.net", "75hosting.org", "7days-printing.com", "7mail.ga",
               "7mail.ml", "7tags.com", "80665.com", "8127ep.com", "8mail.cf", "8mail.ga", "8mail.ml", "99experts.com",
               "9mail.cf", "9ox.net", "a-bc.net", "a45.in", "abakiss.com", "abcmail.email", "abusemail.de", "abuser.eu",
               "abyssmail.com", "ac20mail.in", "academiccommunity.com", "acentri.com", "adiq.eu", "adobeccepdm.com",
               "adpugh.org", "adsd.org", "advantimo.com", "adwaterandstir.com", "aegia.net", "aegiscorp.net", "aelo.es",
               "aeonpsi.com", "afrobacon.com", "agedmail.com", "agger.ro", "agtx.net", "ahk.jp", "airsi.de",
               "ajaxapp.net", "akapost.com", "akerd.com", "al-qaeda.us", "aligamel.com", "alisongamel.com",
               "alivance.com", "alldirectbuy.com", "allowed.org", "allthegoodnamesaretaken.org", "alph.wtf",
               "ama-trade.de", "ama-trans.de", "amail.com", "amail4.me", "amazon-aws.org", "amelabs.com",
               "amilegit.com", "amiri.net", "amiriindustries.com", "ampsylike.com", "anappfor.com", "anappthat.com",
               "andthen.us", "animesos.com", "anit.ro", "ano-mail.net", "anon-mail.de", "anonbox.net", "anonmails.de",
               "anonymail.dk", "anonymbox.com", "anonymized.org", "anonymousness.com", "ansibleemail.com",
               "anthony-junkmail.com", "antireg.com", "antireg.ru", "antispam.de", "antispam24.de", "antispammail.de",
               "apfelkorps.de", "aphlog.com", "appc.se", "appinventor.nl", "appixie.com", "apps.dj", "arduino.hk",
               "armyspy.com", "aron.us", "arroisijewellery.com", "artman-conception.com", "arurgitu.gq",
               "arvato-community.de", "aschenbrandt.net", "asdasd.nl", "asdasd.ru", "ashleyandrew.com",
               "astroempires.info", "asu.mx", "asu.su", "at0mik.org", "augmentationtechnology.com", "auti.st",
               "autorobotica.com", "autotwollow.com", "aver.com", "avls.pt", "awatum.de", "awiki.org", "axiz.org",
               "azcomputerworks.com", "azmeil.tk", "b1of96u.com", "b2cmail.de", "badgerland.eu", "badoop.com",
               "bareed.ws", "barryogorman.com", "bartdevos.be", "basscode.org", "bauwerke-online.com", "bazaaboom.com",
               "bcast.ws", "bcb.ro", "bccto.me", "bearsarefuzzy.com", "beddly.com", "beefmilk.com", "belljonestax.com",
               "benipaula.org", "bestchoiceusedcar.com", "betr.co", "bgx.ro", "bidourlnks.com", "big1.us",
               "bigprofessor.so", "bigstring.com", "bigwhoop.co.za", "bij.pl", "binkmail.com", "bio-muesli.info",
               "bio-muesli.net", "blackmarket.to", "bladesmail.net", "blip.ch", "blogmyway.org", "bluedumpling.info",
               "bluewerks.com", "bobmail.info", "bobmurchison.com", "bofthew.com", "bonobo.email", "bookthemmore.com",
               "bootybay.de", "borged.com", "borged.net", "borged.org", "bot.nu", "boun.cr", "bouncr.com",
               "boxformail.in", "boximail.com", "boxtemp.com.br", "brandallday.net", "brasx.org", "breakthru.com",
               "brefmail.com", "brennendesreich.de", "briggsmarcus.com", "broadbandninja.com", "bsnow.net",
               "bspamfree.org", "bspooky.com", "bst-72.com", "btb-notes.com", "btc.email", "buffemail.com",
               "bugmenever.com", "bugmenot.com", "bulrushpress.com", "bum.net", "bumpymail.com", "bunchofidiots.com",
               "bund.us", "bundes-li.ga", "bunsenhoneydew.com", "burnthespam.info", "burstmail.info",
               "businessbackend.com", "businesssuccessislifesuccess.com", "buspad.org", "buymoreplays.com",
               "buyordie.info", "buyusedlibrarybooks.org", "byebyemail.com", "byespm.com", "byom.de", "c2.hu",
               "c51vsgq.com", "cachedot.net", "californiafitnessdeals.com", "cam4you.cc", "cane.pw", "casualdx.com",
               "cavi.mx", "cbair.com", "cc.liamria", "cdpa.cc", "ceed.se", "cek.pm", "cellurl.com", "centermail.com",
               "centermail.net", "ch.tc", "chacuo.net", "chammy.info", "cheatmail.de", "chickenkiller.com",
               "chielo.com", "childsavetrust.org", "chilkat.com", "chithinh.com", "chogmail.com", "choicemail1.com",
               "chong-mail.com", "chong-mail.net", "chong-mail.org", "chumpstakingdumps.com", "cigar-auctions.com",
               "civx.org", "ckiso.com", "cl-cl.org", "cl0ne.net", "clandest.in", "clipmail.eu", "clixser.com",
               "clrmail.com", "cmail.com", "cmail.net", "cmail.org", "cnamed.com", "cnew.ir", "cnew.ir", "cnmsg.net",
               "cnsds.de", "cobarekyo1.ml", "codeandscotch.com", "codivide.com", "coieo.com", "coldemail.info",
               "com.ar", "compareshippingrates.org", "completegolfswing.com", "comwest.de", "consumerriot.com",
               "coolandwacky.us", "coolimpool.org", "correo.blogos.net", "cosmorph.com", "courrieltemporaire.com",
               "coza.ro", "crankhole.com", "crapmail.org", "crastination.de", "crazespaces.pw", "crazymailing.com",
               "crossroadsmail.com", "cszbl.com", "ctos.ch", "cu.cc", "cubiclink.com", "curryworld.de", "cust.in",
               "cuvox.de", "cylab.org", "d3p.dk", "dab.ro", "dacoolest.com", "daemsteam.com", "daintly.com",
               "dammexe.net", "dandikmail.com", "darkharvestfilms.com", "daryxfox.net", "dash-pads.com", "dataarca.com",
               "datafilehost", "datarca.com", "datazo.ca", "davidkoh.net", "davidlcreative.com", "dayrep.com",
               "dbunker.com", "dcemail.com", "ddcrew.com", "de-a.org", "deadaddress.com", "deadchildren.org",
               "deadfake.cf", "deadfake.ga", "deadfake.ml", "deadfake.tk", "deadspam.com", "deagot.com", "dealja.com",
               "dealrek.com", "deekayen.us", "defomail.com", "degradedfun.net", "delayload.com", "delayload.net",
               "delikkt.de", "der-kombi.de", "derkombi.de", "derluxuswagen.de", "despam.it", "despammed.com",
               "devnullmail.com", "dharmatel.net", "dhm.ro", "dialogus.com", "diapaulpainting.com",
               "digitalmariachis.com", "digitalsanctuary.com", "dildosfromspace.com", "dingbone.com", "discard.cf",
               "discard.email", "discard.ga", "discard.gq", "discard.ml", "discard.tk", "discardmail.com",
               "discardmail.de", "dispo.in", "dispomail.eu", "disposable-email.ml", "disposable.cf", "disposable.ga",
               "disposable.ml", "disposableaddress.com", "disposableemailaddresses.com", "disposableinbox.com",
               "dispose.it", "disposeamail.com", "disposemail.com", "dispostable.com", "divermail.com", "divismail.ru",
               "dlemail.ru", "dnses.ro", "dob.jp", "dodgeit.com", "dodgemail.de", "dodgit.com", "dodgit.org",
               "dodsi.com", "doiea.com", "dolphinnet.net", "domforfb1.tk", "domforfb18.tk", "domforfb19.tk",
               "domforfb2.tk", "domforfb23.tk", "domforfb27.tk", "domforfb29.tk", "domforfb3.tk", "domforfb4.tk",
               "domforfb5.tk", "domforfb6.tk", "domforfb7.tk", "domforfb8.tk", "domforfb9.tk", "domozmail.com",
               "donemail.ru", "dontreg.com", "dontsendmespam.de", "doquier.tk", "dotman.de", "dotmsg.com",
               "dotslashrage.com", "douchelounge.com", "dozvon-spb.ru", "dp76.com", "drdrb.com", "drdrb.net", "dred.ru",
               "drevo.si", "drivetagdev.com", "droolingfanboy.de", "dropcake.de", "droplar.com", "dropmail.me",
               "dspwebservices.com", "duam.net", "dudmail.com", "duk33.com", "dukedish.com", "dump-email.info",
               "dumpandjunk.com", "dumpmail.de", "dumpyemail.com", "durandinterstellar.com", "duskmail.com",
               "dyceroprojects.com", "dz17.net", "e-mail.com", "e-mail.org", "e3z.de", "e4ward.com",
               "easy-trash-mail.com", "easytrashmail.com", "ebeschlussbuch.de", "ecallheandi.com", "edgex.ru",
               "edinburgh-airporthotels.com", "edu.my", "edu.sg", "edv.to", "ee1.pl", "ee2.pl", "eelmail.com",
               "efxs.ca", "einmalmail.de", "einrot.com", "einrot.de", "eintagsmail.de", "elearningjournal.org",
               "electro.mn", "elitevipatlantamodels.com", "email-fake.cf", "email-fake.ga", "email-fake.gq",
               "email-fake.ml", "email-fake.tk", "email-jetable.fr", "email.cbes.net", "email.net", "email60.com",
               "emailage.cf", "emailage.ga", "emailage.gq", "emailage.ml", "emailage.tk", "emaildienst.de",
               "emailgo.de", "emailias.com", "emailigo.de", "emailinfive.com", "emailisvalid.com", "emaillime.com",
               "emailmiser.com", "emailproxsy.com", "emailresort.com", "emails.ga", "emailsensei.com",
               "emailsingularity.net", "emailspam.cf", "emailspam.ga", "emailspam.gq", "emailspam.ml", "emailspam.tk",
               "emailtemporanea.com", "emailtemporanea.net", "emailtemporar.ro", "emailtemporario.com.br",
               "emailthe.net", "emailtmp.com", "emailto.de", "emailwarden.com", "emailx.at.hm", "emailxfer.com",
               "emailz.cf", "emailz.ga", "emailz.gq", "emailz.ml", "emeil.in", "emeil.ir", "emeraldwebmail.com",
               "emil.com", "emkei.cf", "emkei.ga", "emkei.gq", "emkei.ml", "emkei.tk", "eml.pp.ua", "emz.net",
               "enterto.com", "epb.ro", "ephemail.net", "ephemeral.email", "ericjohnson.ml", "ero-tube.org", "esc.la",
               "escapehatchapp.com", "esemay.com", "esgeneri.com", "esprity.com", "etranquil.com", "etranquil.net",
               "etranquil.org", "evanfox.info", "evopo.com", "example.com", "exitstageleft.net", "explodemail.com",
               "express.net.ua", "extremail.ru", "eyepaste.com", "ez.lv", "ezfill.com", "ezstest.com", "f4k.es",
               "facebook-email.cf", "facebook-email.ga", "facebook-email.ml", "facebookmail.gq", "facebookmail.ml",
               "fadingemail.com", "fag.wf", "failbone.com", "faithkills.com", "fake-email.pp.ua", "fake-mail.cf",
               "fake-mail.ga", "fake-mail.ml", "fakedemail.com", "fakeinbox.cf", "fakeinbox.com", "fakeinbox.ga",
               "fakeinbox.ml", "fakeinbox.tk", "fakeinformation.com", "fakemail.fr", "fakemailgenerator.com",
               "fakemailz.com", "fammix.com", "fangoh.com", "fansworldwide.de", "fantasymail.de", "farrse.co.uk",
               "fastacura.com", "fastchevy.com", "fastchrysler.com", "fasternet.biz", "fastkawasaki.com",
               "fastmazda.com", "fastmitsubishi.com", "fastnissan.com", "fastsubaru.com", "fastsuzuki.com",
               "fasttoyota.com", "fastyamaha.com", "fatflap.com", "fdfdsfds.com", "fer-gabon.org", "fettometern.com",
               "fictionsite.com", "fightallspam.com", "figjs.com", "figshot.com", "fiifke.de", "filbert4u.com",
               "filberts4u.com", "film-blog.biz", "filzmail.com", "fir.hk", "fivemail.de", "fixmail.tk", "fizmail.com",
               "fleckens.hu", "flemail.ru", "flowu.com", "flurred.com", "fly-ts.de", "flyinggeek.net", "flyspam.com",
               "foobarbot.net", "footard.com", "forecastertests.com", "forgetmail.com", "fornow.eu", "forspam.net",
               "foxja.com", "foxtrotter.info", "fr.nf", "fr33mail.info", "frapmail.com", "free-email.cf",
               "free-email.ga", "freebabysittercam.com", "freeblackbootytube.com", "freecat.net", "freedompop.us",
               "freefattymovies.com", "freeletter.me", "freemail.hu", "freemail.ms", "freemails.cf", "freemails.ga",
               "freemails.ml", "freeplumpervideos.com", "freeschoolgirlvids.com", "freesistercam.com",
               "freeteenbums.com", "freundin.ru", "friendlymail.co.uk", "front14.org", "ftp.sh", "ftpinc.ca",
               "fuckedupload.com", "fuckingduh.com", "fudgerub.com", "fuirio.com", "funnycodesnippets.com",
               "furzauflunge.de", "fux0ringduh.com", "fxnxs.com", "fyii.de", "g4hdrop.us", "gaggle.net", "galaxy.tv",
               "gally.jp", "gamegregious.com", "garbagecollector.org", "garbagemail.org", "gardenscape.ca",
               "garizo.com", "garliclife.com", "garrymccooey.com", "gav0.com", "gawab.com",
               "gehensiemirnichtaufdensack.de", "geldwaschmaschine.de", "gelitik.in", "genderfuck.net", "geschent.biz",
               "get-mail.cf", "get-mail.ga", "get-mail.ml", "get-mail.tk", "get1mail.com", "get2mail.fr",
               "getairmail.cf", "getairmail.com", "getairmail.ga", "getairmail.gq", "getairmail.ml", "getairmail.tk",
               "geteit.com", "getmails.eu", "getonemail.com", "getonemail.net", "ghosttexter.de", "giaiphapmuasam.com",
               "giantmail.de", "ginzi.be", "ginzi.co.uk", "ginzi.es", "ginzi.net", "ginzy.co.uk", "ginzy.eu",
               "girlsindetention.com", "girlsundertheinfluence.com", "gishpuppy.com", "glitch.sx", "globaltouron.com",
               "glucosegrin.com", "gmal.com", "gmial.com", "gmx.us", "gnctr-calgary.com", "goemailgo.com", "gomail.in",
               "gorillaswithdirtyarmpits.com", "gothere.biz", "gotmail.com", "gotmail.net", "gotmail.org",
               "gowikibooks.com", "gowikicampus.com", "gowikicars.com", "gowikifilms.com", "gowikigames.com",
               "gowikimusic.com", "gowikinetwork.com", "gowikitravel.com", "gowikitv.com", "grandmamail.com",
               "grandmasmail.com", "great-host.in", "greensloth.com", "greggamel.com", "greggamel.net",
               "gregorsky.zone", "gregorygamel.com", "gregorygamel.net", "grish.de", "grr.la", "gs-arc.org",
               "gsredcross.org", "gsrv.co.uk", "gudanglowongan.com", "guerillamail.biz", "guerillamail.com",
               "guerillamail.de", "guerillamail.info", "guerillamail.net", "guerillamail.org", "guerillamailblock.com",
               "guerrillamail.biz", "guerrillamail.com", "guerrillamail.de", "guerrillamail.info", "guerrillamail.net",
               "guerrillamail.org", "guerrillamailblock.com", "gustr.com", "gynzi.co.uk", "gynzi.es", "gynzy.at",
               "gynzy.es", "gynzy.eu", "gynzy.gr", "gynzy.info", "gynzy.lt", "gynzy.mobi", "gynzy.pl", "gynzy.ro",
               "gynzy.sk", "gzb.ro", "h8s.org", "habitue.net", "hacccc.com", "hackthatbit.ch", "hahawrong.com",
               "haltospam.com", "harakirimail.com", "haribu.com", "hartbot.de", "hat-geld.de", "hatespam.org",
               "hawrong.com", "hazelnut4u.com", "hazelnuts4u.com", "hazmatshipping.org", "headstrong.de",
               "heathenhammer.com", "heathenhero.com", "hecat.es", "hellodream.mobi", "helloricky.com",
               "helpinghandtaxcenter.org", "herp.in", "herpderp.nl", "hi5.si", "hiddentragedy.com", "hidemail.de",
               "hidzz.com", "highbros.org", "hmail.us", "hmamail.com", "hmh.ro", "hoanggiaanh.com", "hochsitze.com",
               "hopemail.biz", "hot-mail.cf", "hot-mail.ga", "hot-mail.gq", "hot-mail.ml", "hot-mail.tk", "hotmai.com",
               "hotmial.com", "hotpop.com", "hpc.tw", "hs.vc", "ht.cx", "hulapla.de", "humaility.com", "humn.ws.gy",
               "hungpackage.com", "huskion.net", "hvastudiesucces.nl", "hwsye.net", "ibnuh.bz",
               "icantbelieveineedtoexplainthisshit.com", "icx.in", "icx.ro", "id.au", "ieatspam.eu", "ieatspam.info",
               "ieh-mail.de", "ige.es", "ignoremail.com", "ihateyoualot.info", "iheartspam.org", "ikbenspamvrij.nl",
               "illistnoise.com", "ilovespam.com", "imails.info", "imgof.com", "imgv.de", "imstations.com", "inbax.tk",
               "inbound.plus", "inbox.si", "inbox2.info", "inboxalias.com", "inboxclean.com", "inboxclean.org",
               "inboxdesign.me", "inboxed.im", "inboxed.pw", "inboxproxy.com", "inboxstore.me", "inclusiveprogress.com",
               "incognitomail.com", "incognitomail.net", "incognitomail.org", "incq.com", "ind.st", "indieclad.com",
               "indirect.ws", "ineec.net", "infocom.zp.ua", "inggo.org", "inoutmail.de", "inoutmail.eu",
               "inoutmail.info", "inoutmail.net", "insanumingeniumhomebrew.com", "insorg-mail.info", "instant-mail.de",
               "instantemailaddress.com", "internetoftags.com", "interstats.org", "intersteller.com", "iozak.com",
               "ip6.li", "ipoo.org", "ipsur.org", "irc.so", "irish2me.com", "iroid.com", "ironiebehindert.de",
               "irssi.tv", "is.af", "isukrainestillacountry.com", "it7.ovh", "itunesgiftcodegenerator.com", "iwi.net",
               "ixx.io", "j-p.us", "j.svxr.org", "jafps.com", "jdmadventures.com", "jdz.ro", "jellyrolls.com",
               "jetable.com", "jetable.fr.nf", "jetable.net", "jetable.org", "jetable.pp.ua", "jmail.ro", "jnxjn.com",
               "jobbikszimpatizans.hu", "jobposts.net", "jobs-to-be-done.net", "joelpet.com", "joetestalot.com",
               "jopho.com", "jourrapide.com", "jpco.org", "jsrsolutions.com", "jungkamushukum.com", "junk.to",
               "junk1e.com", "junkmail.ga", "junkmail.gq", "jwork.ru", "kakadua.net", "kalapi.org", "kamsg.com",
               "kaovo.com", "kariplan.com", "kartvelo.com", "kasmail.com", "kaspop.com", "kcrw.de", "keepmymail.com",
               "keinhirn.de", "keipino.de", "kemptvillebaseball.com", "kennedy808.com", "kiani.com", "killmail.com",
               "killmail.net", "kimsdisk.com", "kingsq.ga", "kiois.com", "kismail.ru", "kisstwink.com", "kitnastar.com",
               "klassmaster.com", "klassmaster.net", "kloap.com", "kludgemush.com", "klzlk.com", "kmhow.com",
               "kommunity.biz", "kon42.com", "kook.ml", "kopagas.com", "kopaka.net", "kosmetik-obatkuat.com",
               "kostenlosemailadresse.de", "koszmail.pl", "krypton.tk", "kuhrap.com", "kulturbetrieb.info",
               "kurzepost.de", "kwift.net", "kwilco.net", "kyal.pl", "l-c-a.us", "l33r.eu", "labetteraverouge.at",
               "lackmail.net", "lackmail.ru", "lags.us", "lain.ch", "lakelivingstonrealestate.com", "landmail.co",
               "laoeq.com", "lastmail.co", "lastmail.com", "lawlita.com", "lazyinbox.com", "ldop.com", "ldtp.com",
               "lee.mx", "leeching.net", "lellno.gq", "letmeinonthis.com", "letthemeatspam.com", "lez.se", "lhsdv.com",
               "liamcyrus.com", "lifebyfood.com", "lifetotech.com", "ligsb.com", "lilo.me", "lindenbaumjapan.com",
               "link2mail.net", "linkedintuts2016.pw", "linuxmail.so", "litedrop.com", "lkgn.se", "llogin.ru",
               "loadby.us", "locomodev.net", "login-email.cf", "login-email.ga", "login-email.ml", "login-email.tk",
               "logular.com", "loin.in", "lolfreak.net", "lolmail.biz", "lookugly.com", "lopl.co.cc", "lortemail.dk",
               "losemymail.com", "lovemeleaveme.com", "lpfmgmtltd.com", "lr7.us", "lr78.com", "lroid.com", "lru.me",
               "luckymail.org", "lukecarriere.com", "lukemail.info", "lukop.dk", "luv2.us",
               "lyfestylecreditsolutions.com", "m21.cc", "m4ilweb.info", "maboard.com", "macromaid.com", "magamail.com",
               "magicbox.ro", "maidlow.info", "mail-filter.com", "mail-owl.com", "mail-temporaire.com",
               "mail-temporaire.fr", "mail.by", "mail114.net", "mail1a.de", "mail21.cc", "mail2rss.org", "mail333.com",
               "mail4trash.com", "mail666.ru", "mail707.com", "mail72.com", "mailback.com", "mailbidon.com",
               "mailbiz.biz", "mailblocks.com", "mailbucket.org", "mailcat.biz", "mailcatch.com", "mailchop.com",
               "mailcker.com", "mailde.de", "mailde.info", "maildrop.cc", "maildrop.cf", "maildrop.ga", "maildrop.gq",
               "maildrop.ml", "maildu.de", "maildx.com", "maileater.com", "mailed.in", "mailed.ro", "maileimer.de",
               "mailexpire.com", "mailfa.tk", "mailforspam.com", "mailfree.ga", "mailfree.gq", "mailfree.ml",
               "mailfreeonline.com", "mailfs.com", "mailguard.me", "mailhazard.com", "mailhazard.us", "mailhz.me",
               "mailimate.com", "mailin8r.com", "mailinatar.com", "mailinater.com", "mailinator.co.uk",
               "mailinator.com", "mailinator.gq", "mailinator.info", "mailinator.net", "mailinator.org",
               "mailinator.us", "mailinator2.com", "mailincubator.com", "mailismagic.com", "mailita.tk", "mailjunk.cf",
               "mailjunk.ga", "mailjunk.gq", "mailjunk.ml", "mailjunk.tk", "mailmate.com", "mailme.gq", "mailme.ir",
               "mailme.lv", "mailme24.com", "mailmetrash.com", "mailmoat.com", "mailms.com", "mailnator.com",
               "mailnesia.com", "mailnull.com", "mailonaut.com", "mailorc.com", "mailorg.org", "mailpick.biz",
               "mailproxsy.com", "mailquack.com", "mailrock.biz", "mailsac.com", "mailscrap.com", "mailseal.de",
               "mailshell.com", "mailsiphon.com", "mailslapping.com", "mailslite.com", "mailtemp.info",
               "mailtemporaire.com", "mailtemporaire.fr", "mailtome.de", "mailtothis.com", "mailtrash.net",
               "mailtv.net", "mailtv.tv", "mailzi.ru", "mailzilla.com", "mailzilla.org", "mailzilla.orgmbx.cc",
               "makemetheking.com", "malahov.de", "malayalamdtp.com", "manifestgenerator.com", "mansiondev.com",
               "manybrain.com", "markmurfin.com", "mbx.cc", "mcache.net", "mciek.com", "meepsheep.eu",
               "meinspamschutz.de", "meltmail.com", "messagebeamer.de", "messwiththebestdielikethe.rest",
               "mezimages.net", "mfsa.ru", "miaferrari.com", "midcoastcustoms.com", "midcoastcustoms.net",
               "midcoastsolutions.com", "midcoastsolutions.net", "midlertidig.com", "midlertidig.net",
               "midlertidig.org", "mierdamail.com", "migmail.net", "migmail.pl", "migumail.com", "mijnhva.nl",
               "ministry-of-silly-walks.de", "minsmail.com", "mintemail.com", "misterpinball.de", "mji.ro",
               "mjukglass.nu", "mkpfilm.com", "ml8.ca", "mm.my", "mm5.se", "moakt.com", "moakt.ws", "mobileninja.co.uk",
               "moburl.com", "mockmyid.com", "moeri.org", "mohmal.com", "momentics.ru", "moneypipe.net",
               "monumentmail.com", "moonwake.com", "moot.es", "moreawesomethanyou.com", "moreorcs.com", "motique.de",
               "mountainregionallibrary.net", "moza.pl", "msgos.com", "msk.ru", "mspeciosa.com", "mswork.ru",
               "msxd.com", "mt2009.com", "mt2014.com", "mt2015.com", "mtmdev.com", "muathegame.com", "muchomail.com",
               "mucincanon.com", "mutant.me", "mvrht.com", "mwarner.org", "mxfuel.com", "my10minutemail.com",
               "mybitti.de", "mycleaninbox.net", "mycorneroftheinter.net", "mydemo.equipment", "myecho.es",
               "myemailboxy.com", "mykickassideas.com", "mymail-in.net", "mymailoasis.com", "mynetstore.de",
               "myopang.com", "mypacks.net", "mypartyclip.de", "myphantomemail.com", "mysamp.de", "myspaceinc.com",
               "myspaceinc.net", "myspaceinc.org", "myspacepimpedup.com", "myspamless.com", "mytemp.email",
               "mytempemail.com", "mytempmail.com", "mytrashmail.com", "mywarnernet.net", "myzx.com", "n1nja.org",
               "nabuma.com", "nakedtruth.biz", "nanonym.ch", "nationalgardeningclub.com", "naver.com", "negated.com",
               "neomailbox.com", "nepwk.com", "nervmich.net", "nervtmich.net", "net.ua", "netmails.com", "netmails.net",
               "netricity.nl", "netris.net", "netviewer-france.com", "netzidiot.de", "nevermail.de",
               "nextstopvalhalla.com", "nfast.net", "nguyenusedcars.com", "nh3.ro", "nice-4u.com", "nicknassar.com",
               "nincsmail.hu", "niwl.net", "nm7.cc", "nmail.cf", "nnh.com", "nnot.net", "no-spam.ws", "no-ux.com",
               "noblepioneer.com", "nobugmail.com", "nobulk.com", "nobuma.com", "noclickemail.com", "nodezine.com",
               "nogmailspam.info", "nokiamail.com", "nom.za", "nomail.pw", "nomail2me.com", "nomorespamemails.com",
               "nonspam.eu", "nonspammer.de", "nonze.ro", "noref.in", "norseforce.com", "nospam.ze.tc", "nospam4.us",
               "nospamfor.us", "nospamthanks.info", "nothingtoseehere.ca", "notmailinator.com", "notrnailinator.com",
               "notsharingmy.info", "now.im", "nowhere.org", "nowmymail.com", "ntlhelp.net", "nubescontrol.com",
               "nullbox.info", "nurfuerspam.de", "nuts2trade.com", "nwldx.com", "ny7.me", "o2stk.org", "o7i.net",
               "obfusko.com", "objectmail.com", "obobbo.com", "obxpestcontrol.com", "odaymail.com", "odnorazovoe.ru",
               "oerpub.org", "offshore-proxies.net", "ohaaa.de", "ohi.tw", "okclprojects.com", "okrent.us", "okzk.com",
               "olypmall.ru", "omail.pro", "omnievents.org", "one-time.email", "oneoffemail.com", "oneoffmail.com",
               "onet.pl", "onewaymail.com", "onlatedotcom.info", "online.ms", "onlineidea.info", "onqin.com",
               "ontyne.biz", "oolus.com", "oopi.org", "opayq.com", "opp24.com", "ordinaryamerican.net", "org.ua",
               "oroki.de", "oshietechan.link", "otherinbox.com", "ourklips.com", "ourpreviewdomain.com",
               "outlawspam.com", "ovpn.to", "owlpic.com", "ownsyou.de", "oxopoha.com", "ozyl.de", "pa9e.com",
               "pagamenti.tk", "pancakemail.com", "paplease.com", "pastebitch.com", "pcusers.otherinbox.com",
               "penisgoes.in", "pepbot.com", "peterdethier.com", "petrzilka.net", "pfui.ru", "photomark.net",
               "phpbb.uu.gl", "pi.vu", "pii.at", "piki.si", "pimpedupmyspace.com", "pinehill-seattle.org", "pingir.com",
               "pisls.com", "pjjkp.com", "plexolan.de", "plhk.ru", "plw.me", "pojok.ml", "pokiemobile.com",
               "politikerclub.de", "pooae.com", "poofy.org", "pookmail.com", "poopiebutt.club", "popesodomy.com",
               "popgx.com", "postacin.com", "postonline.me", "poutineyourface.com", "powered.name", "powlearn.com",
               "pp.ua", "primabananen.net", "privacy.net", "privatdemail.net", "privy-mail.com", "privy-mail.de",
               "privymail.de", "pro-tag.org", "procrackers.com", "projectcl.com", "propscore.com", "proxymail.eu",
               "proxyparking.com", "prtnx.com", "prtz.eu", "psh.me", "punkass.com", "purcell.email",
               "purelogistics.org", "put2.net", "putthisinyourspamdatabase.com", "pwrby.com", "qasti.com", "qc.to",
               "qibl.at", "qipmail.net", "qisdo.com", "qisoa.com", "qoika.com", "qq.my", "qsl.ro", "quadrafit.com",
               "quickinbox.com", "quickmail.nl", "qvy.me", "qwickmail.com", "r4nd0m.de", "ra3.us", "rabin.ca",
               "raetp9.com", "raketenmann.de", "rancidhome.net", "randomail.net", "raqid.com", "rax.la", "raxtest.com",
               "rbb.org", "rcpt.at", "reallymymail.com", "realtyalerts.ca", "receiveee.com", "recipeforfailure.com",
               "recode.me", "reconmail.com", "recyclemail.dk", "redfeathercrow.com", "regbypass.com", "rejectmail.com",
               "reliable-mail.com", "remail.cf", "remail.ga", "remarkable.rocks", "remote.li", "reptilegenetics.com",
               "revolvingdoorhoax.org", "rhyta.com", "riddermark.de", "risingsuntouch.com", "rklips.com", "rma.ec",
               "rmqkr.net", "rnailinator.com", "ro.lt", "robertspcrepair.com", "ronnierage.net", "rotaniliam.com",
               "rowe-solutions.com", "royal.net", "royaldoodles.org", "rppkn.com", "rtrtr.com", "ruffrey.com",
               "rumgel.com", "runi.ca", "rustydoor.com", "rvb.ro", "s0ny.net", "s33db0x.com", "sabrestlouis.com",
               "sackboii.com", "safersignup.de", "safetymail.info", "safetypost.de", "saharanightstempe.com",
               "samsclass.info", "sandelf.de", "sandwhichvideo.com", "sanfinder.com", "sanim.net", "sanstr.com",
               "sast.ro", "satukosong.com", "sausen.com", "saynotospams.com", "scatmail.com", "scay.net",
               "schachrol.com", "schafmail.de", "schmeissweg.tk", "schrott-email.de", "sd3.in", "secmail.pw",
               "secretemail.de", "secure-mail.biz", "secure-mail.cc", "secured-link.net", "securehost.com.es",
               "seekapps.com", "sejaa.lv", "selfdestructingmail.com", "selfdestructingmail.org", "sendfree.org",
               "sendingspecialflyers.com", "sendspamhere.com", "senseless-entertainment.com", "server.ms",
               "services391.com", "sexforswingers.com", "sexical.com", "sharedmailbox.org", "sharklasers.com",
               "shhmail.com", "shhuut.org", "shieldedmail.com", "shieldemail.com", "shiftmail.com", "shipfromto.com",
               "shiphazmat.org", "shipping-regulations.com", "shippingterms.org", "shitmail.de", "shitmail.me",
               "shitmail.org", "shitware.nl", "shmeriously.com", "shortmail.net", "shotmail.ru", "showslow.de",
               "shrib.com", "shut.name", "shut.ws", "sibmail.com", "sify.com", "simpleitsecurity.info", "sin.cl",
               "sinfiltro.cl", "singlespride.com", "sinnlos-mail.de", "sino.tw", "siteposter.net",
               "sizzlemctwizzle.com", "skeefmail.com", "sky-inbox.com", "sky-ts.de", "slapsfromlastnight.com",
               "slaskpost.se", "slave-auctions.net", "slopsbox.com", "slothmail.net", "slushmail.com", "sly.io",
               "smapfree24.com", "smapfree24.de", "smapfree24.eu", "smapfree24.info", "smapfree24.org", "smashmail.de",
               "smellfear.com", "smellrear.com", "smtp99.com", "smwg.info", "snakemail.com", "sneakemail.com",
               "sneakmail.de", "snkmail.com", "socialfurry.org", "sofimail.com", "sofort-mail.de", "sofortmail.de",
               "softpls.asia", "sogetthis.com", "sohu.com", "soisz.com", "solvemail.info", "solventtrap.wiki",
               "soodmail.com", "soodomail.com", "soodonims.com", "soon.it", "spam-be-gone.com", "spam.la",
               "spam.org.es", "spam.su", "spam4.me", "spamail.de", "spamarrest.com", "spamavert.com", "spambob.com",
               "spambob.net", "spambob.org", "spambog.com", "spambog.de", "spambog.net", "spambog.ru", "spambooger.com",
               "spambox.info", "spambox.irishspringrealty.com", "spambox.org", "spambox.us", "spamcero.com",
               "spamcon.org", "spamcorptastic.com", "spamcowboy.com", "spamcowboy.net", "spamcowboy.org", "spamday.com",
               "spamdecoy.net", "spamex.com", "spamfighter.cf", "spamfighter.ga", "spamfighter.gq", "spamfighter.ml",
               "spamfighter.tk", "spamfree.eu", "spamfree24.com", "spamfree24.de", "spamfree24.eu", "spamfree24.info",
               "spamfree24.net", "spamfree24.org", "spamgoes.in", "spamherelots.com", "spamhereplease.com",
               "spamhole.com", "spamify.com", "spaminator.de", "spamkill.info", "spaml.com", "spaml.de", "spamlot.net",
               "spammotel.com", "spamobox.com", "spamoff.de", "spamsalad.in", "spamslicer.com", "spamspot.com",
               "spamstack.net", "spamthis.co.uk", "spamthisplease.com", "spamtrail.com", "spamtroll.net", "spb.ru",
               "speed.1s.fr", "speedgaus.net", "spikio.com", "spoofmail.de", "spr.io", "spritzzone.de", "spybox.de",
               "squizzy.de", "sry.li", "ssoia.com", "stanfordujjain.com", "starlight-breaker.net", "startfu.com",
               "startkeys.com", "statdvr.com", "stathost.net", "statiix.com", "steambot.net", "stexsy.com",
               "stinkefinger.net", "stop-my-spam.cf", "stop-my-spam.com", "stop-my-spam.ga", "stop-my-spam.ml",
               "stop-my-spam.tk", "streetwisemail.com", "stuckmail.com", "stuffmail.de", "stumpfwerk.com",
               "suburbanthug.com", "suckmyd.com", "sudolife.me", "sudolife.net", "sudomail.biz", "sudomail.com",
               "sudomail.net", "sudoverse.com", "sudoverse.net", "sudoweb.net", "sudoworld.com", "sudoworld.net",
               "suioe.com", "super-auswahl.de", "supergreatmail.com", "supermailer.jp", "superplatyna.com",
               "superrito.com", "superstachel.de", "suremail.info", "svk.jp", "svxr.org", "sweetxxx.de",
               "swift10minutemail.com", "sylvannet.com", "tafmail.com", "tafoi.gr", "tagmymedia.com", "tagyourself.com",
               "talkinator.com", "tanukis.org", "tapchicuoihoi.com", "tb-on-line.net", "techemail.com", "techgroup.me",
               "teewars.org", "tefl.ro", "telecomix.pl", "teleworm.com", "teleworm.us", "temp-mail.com", "temp-mail.de",
               "temp-mail.org", "temp-mail.ru", "tempail.com", "tempalias.com", "tempe-mail.com", "tempemail.biz",
               "tempemail.co.za", "tempemail.com", "tempemail.net", "tempinbox.co.uk", "tempinbox.com", "tempmail.co",
               "tempmail.de", "tempmail.eu", "tempmail.it", "tempmail.us", "tempmail2.com", "tempmaildemo.com",
               "tempmailer.com", "tempmailer.de", "tempomail.fr", "temporarily.de", "temporarioemail.com.br",
               "temporaryemail.net", "temporaryemail.us", "temporaryforwarding.com", "temporaryinbox.com",
               "temporarymailaddress.com", "tempsky.com", "tempthe.net", "tempymail.com", "testudine.com",
               "thanksnospam.info", "thankyou2010.com", "thc.st", "theaviors.com", "thebearshark.com",
               "thecloudindex.com", "thediamants.org", "thelimestones.com", "thembones.com.au", "themostemail.com",
               "thereddoors.online", "thescrappermovie.com", "theteastory.info", "thex.ro", "thietbivanphong.asia",
               "thisisnotmyrealemail.com", "thismail.net", "thisurl.website", "thnikka.com", "thraml.com", "thrma.com",
               "throam.com", "thrott.com", "throwam.com", "throwawayemailaddress.com", "throwawaymail.com",
               "thunkinator.org", "thxmate.com", "tic.ec", "tilien.com", "timgiarevn.com", "timkassouf.com",
               "tinyurl24.com", "tittbit.in", "tiv.cc", "tizi.com", "tkitc.de", "tlpn.org", "tmail.com", "tmail.ws",
               "tmailinator.com", "tmpjr.me", "toddsbighug.com", "toiea.com", "tokem.co", "tokenmail.de",
               "tonymanso.com", "toomail.biz", "top101.de", "top1mail.ru", "top1post.ru", "topofertasdehoy.com",
               "topranklist.de", "toprumours.com", "tormail.org", "toss.pw", "tosunkaya.com", "totalvista.com",
               "totesmail.com", "tp-qa-mail.com", "tqoai.com", "tradermail.info", "tranceversal.com", "trash-amil.com",
               "trash-mail.at", "trash-mail.cf", "trash-mail.com", "trash-mail.de", "trash-mail.ga", "trash-mail.gq",
               "trash-mail.ml", "trash-mail.tk", "trash2009.com", "trash2010.com", "trash2011.com", "trashcanmail.com",
               "trashdevil.com", "trashdevil.de", "trashemail.de", "trashinbox.com", "trashmail.at", "trashmail.com",
               "trashmail.de", "trashmail.me", "trashmail.net", "trashmail.org", "trashmail.ws", "trashmailer.com",
               "trashymail.com", "trashymail.net", "trasz.com", "trayna.com", "trbvm.com", "trbvn.com", "trbvo.com",
               "trialmail.de", "trickmail.net", "trillianpro.com", "trollproject.com", "tropicalbass.info",
               "trungtamtoeic.com", "tryalert.com", "ttszuo.xyz", "tualias.com", "turoid.com", "turual.com",
               "twinmail.de", "twoweirdtricks.com", "txtadvertise.com", "tyhe.ro", "tyldd.com", "ubismail.net",
               "ubm.md", "ufacturing.com", "uggsrock.com", "uguuchantele.com", "uhhu.ru", "uk.to", "umail.net",
               "undo.it", "unimark.org", "unit7lahaina.com", "unmail.ru", "upliftnow.com", "uplipht.com",
               "uploadnolimit.com", "urfunktion.se", "uroid.com", "us.af", "us.to", "utiket.us", "uu.gl", "uwork4.us",
               "uyhip.com", "vaati.org", "valemail.net", "valhalladev.com", "vankin.de", "vda.ro", "vdig.com",
               "venompen.com", "verdejo.com", "veryday.ch", "veryday.eu", "veryday.info", "veryrealemail.com",
               "vesa.pw", "vfemail.net", "victime.ninja", "victoriantwins.com", "vidchart.com", "viditag.com",
               "viewcastmedia.com", "viewcastmedia.net", "viewcastmedia.org", "vikingsonly.com", "vinernet.com",
               "vipmail.name", "vipmail.pw", "vipxm.net", "viralplays.com", "vixletdev.com", "vkcode.ru",
               "vmailing.info", "vmani.com", "vmpanda.com", "voidbay.com", "vomoto.com", "vorga.org", "votiputox.org",
               "voxelcore.com", "vpn.st", "vrmtr.com", "vsimcard.com", "vubby.com", "vztc.com", "w3internet.co.uk",
               "wakingupesther.com", "walala.org", "walkmail.net", "walkmail.ru", "wallm.com", "wasteland.rfc822.org",
               "watch-harry-potter.com", "watchever.biz", "watchfull.net", "watchironman3onlinefreefullmovie.com",
               "wbml.net", "web-mail.pp.ua", "web.id", "webemail.me", "webm4il.info", "webtrip.ch", "webuser.in",
               "wee.my", "wef.gr", "wefjo.grn.cc", "weg-werf-email.de", "wegwerf-email-addressen.de",
               "wegwerf-email-adressen.de", "wegwerf-email.de", "wegwerf-email.net", "wegwerf-emails.de",
               "wegwerfadresse.de", "wegwerfemail.com", "wegwerfemail.de", "wegwerfemail.net", "wegwerfemail.org",
               "wegwerfemailadresse.com", "wegwerfmail.de", "wegwerfmail.info", "wegwerfmail.net", "wegwerfmail.org",
               "wegwerpmailadres.nl", "wegwrfmail.de", "wegwrfmail.net", "wegwrfmail.org", "welikecookies.com",
               "wetrainbayarea.com", "wetrainbayarea.org", "wg0.com", "wh4f.org", "whatiaas.com", "whatifanalytics.com",
               "whatpaas.com", "whatsaas.com", "whiffles.org", "whopy.com", "whyspam.me", "wibblesmith.com",
               "wickmail.net", "widget.gg", "wilemail.com", "willhackforfood.biz", "willselfdestruct.com", "wimsg.com",
               "winemaven.info", "wins.com.br", "wmail.cf", "wolfsmail.tk", "wollan.info", "worldspace.link", "wpg.im",
               "wralawfirm.com", "writeme.us", "wronghead.com", "wuzup.net", "wuzupmail.net", "wwwnew.eu", "wxnw.net",
               "x24.com", "xagloo.co", "xagloo.com", "xcompress.com", "xcpy.com", "xemaps.com", "xents.com",
               "xing886.uu.gl", "xjoi.com", "xl.cx", "xmail.com", "xmaily.com", "xn--9kq967o.com", "xoxox.cc",
               "xrho.com", "xwaretech.com", "xwaretech.info", "xwaretech.net", "xww.ro", "xyzfree.net", "xzsok.com",
               "yanet.me", "yapped.net", "yaqp.com", "ycare.de", "ycn.ro", "ye.vc", "yedi.org", "yep.it", "yhg.biz",
               "ynmrealty.com", "yodx.ro", "yogamaven.com", "yomail.info", "yoo.ro", "yopmail.com", "yopmail.fr",
               "yopmail.gq", "yopmail.net", "you-spam.com", "yougotgoated.com", "youmail.ga", "youmailr.com",
               "youneedmore.info", "yourdomain.com", "yourewronghereswhy.com", "yourlms.biz", "yspend.com",
               "yugasandrika.com", "yui.it", "yuurok.com", "yxzx.net", "z0d.eu", "z1p.biz", "z86.ru", "za.com",
               "zasod.com", "zebins.com", "zebins.eu", "zehnminuten.de", "zehnminutenmail.de", "zepp.dk", "zetmail.com",
               "zfymail.com", "zik.dj", "zippymail.info", "zipsendtest.com", "zoaxe.com", "zoemail.com", "zoemail.net",
               "zoemail.org", "zoetropes.org", "zombie-hive.com", "zomg.info", "zp.ua", "zumpul.com", "zxcv.com",
               "zxcvbnm.com", "zzz.com"]


def interactive_check():
    import time

    try:  # py2
        raw_input
    except NameError:  # py3
        def raw_input(prompt=''):
            return input(prompt)

    while True:
        email = raw_input('Enter email for validation: ')

        mx = raw_input('Validate MX record? [yN] ')
        if mx.strip().lower() == 'y':
            mx = True
        else:
            mx = False

        validate = raw_input('Try to contact server for address validation? [yN] ')
        if validate.strip().lower() == 'y':
            validate = True
        else:
            validate = False

        disposable = raw_input('Can the email be disposable? [Yn] ')
        if disposable.strip().lower() == 'n':
            disposable = False
        else:
            disposable = True

        sending_email = raw_input('sending_email? [string] ')

        logging.basicConfig()

        result = validate_email(email, mx, validate, debug=True, smtp_timeout=1,
                                allow_disposable=disposable,
                                sending_email=sending_email, sql_conn=None)
        if result:
            print("Valid!")
        elif result is None:
            print("I'm not sure.")
        else:
            print("Invalid!")

        time.sleep(1)


if __name__ == "__main__":
    interactive_check()
