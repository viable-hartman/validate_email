#!/usr/bin/env python

# Logger imports
import logging
import sys

# Setup logger
logger = logging.getLogger('create_db')
logger.setLevel(logging.DEBUG)       # Development
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.DEBUG)           # Development
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)

import sqlite3
conn = sqlite3.connect('validate_email.db')

c = conn.cursor()

# Create known_domains table to quickly look up known domains.
c.execute('''CREATE TABLE IF NOT EXISTS known_domains
             (id integer primary key, domain varchar unique, desc text)''')

# Populate the known_domains table with known data.
known_domains = [(1, 'gmail.com','Domain for addresses like example@gmail.com'),
                 (2, 'yahoo.com','Domain for addresses like example@yahoo.com'),
                 (3, 'hotmail.com','Domain for addresses like example@hotmail.com'),
                 (4, 'live.com','Domain for addresses like example@live.com'),
                ]
try:
    c.executemany('INSERT INTO known_domains VALUES (?,?,?)', known_domains)
except sqlite3.IntegrityError as ie:
    logger.debug(str(ie))

# Create the servers table with a list of known SMTP servers to map to domains.
c.execute('''CREATE TABLE IF NOT EXISTS servers
             (id integer primary key, server varchar unique, desc text)''')

# Populate the servers table with known data.
servers = [(1, 'smtp.gmail.com', 'SMTP VIP for Google E-Mail Addresses.'),
           (2, 'smtp.mail.yahoo.com', 'SMTP VIP for Yahoo E-Mail Addresses'),
           (3, 'smtp.live.com', 'TLS SMTP VIP for Hotmail and Windows Live E-Mail Addresses'),
          ]
try:
    c.executemany('INSERT INTO servers VALUES (?,?,?)', servers)
except sqlite3.IntegrityError as ie:
    logger.debug(str(ie))

# Create the creds table with our stored credentials for various mail services.
c.execute('''CREATE TABLE IF NOT EXISTS creds
             (id integer primary key, username varchar, password varchar, token varchar, desc text)''')

# Populate the creds table with existing credential data.
creds = [(0, 'No Auth Required', None, None, 'No Authentication / Authorization Required.'),
         (1, 'VALID_GMAIL_ACCOUNT@gmail.com', 'VALID_PASS', None, 'Sending user for authenticating to Google services.'),
         (2, 'VALID_YAHOO_ACCOUNT@yahoo.com', 'VALID_PASS', None, 'Sending user for authenticating to Yahoo services.'),
         (3, 'VALID_LIVE_ACCOUNT@[hotmail|live].com', 'VALID_PASS', None, 'Sending user for authenticating to Windows Live/Hotmail services.'),
        ]
try:
    c.executemany('INSERT INTO creds VALUES (?,?,?,?,?)', creds)
except sqlite3.IntegrityError as ie:
    logger.debug(str(ie))

# Create connections table used for linking known domains to servers with proper connection options.
c.execute('''CREATE TABLE IF NOT EXISTS connections
             (domain_id integer,
              server_id integer,
              creds_id integer default 0,
              ssl integer default 1,
              port integer default 465,
              PRIMARY KEY (domain_id, server_id),
              FOREIGN KEY (domain_id) REFERENCES known_domains(id) ON UPDATE CASCADE ON DELETE CASCADE,
              FOREIGN KEY (server_id) REFERENCES servers(id) ON UPDATE CASCADE ON DELETE CASCADE,
              FOREIGN KEY (creds_id) REFERENCES creds(id) ON UPDATE CASCADE ON DELETE CASCADE
             )''')

# Populate the connections table with known connections.
conn_info = [(1, 1, 1, 1, 465),
             (2, 2, 2, 1, 465),
             (3, 3, 3, 1, 587),
             (4, 3, 3, 1, 587),
            ]
try:
    c.executemany('INSERT INTO connections VALUES (?,?,?,?,?)', conn_info)
except sqlite3.IntegrityError as ie:
    logger.debug(str(ie))

# Create a view for all credentials.
c.execute('''CREATE VIEW IF NOT EXISTS connectionView AS 
                 SELECT kd.domain, s.server, cr.username, cr.password, c.ssl, c.port
                 FROM known_domains AS kd
                     INNER JOIN connections AS c ON kd.id = c.domain_id
                     INNER JOIN servers AS s ON c.server_id = s.id
                     INNER JOIN creds AS cr ON c.creds_id = cr.id''')

# Save everything.
conn.commit()

# Saefely close the DB conn
conn.close()
