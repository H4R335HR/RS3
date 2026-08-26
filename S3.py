import urllib.request, urllib.parse, urllib.error, re, html, webbrowser, hashlib, time, _thread, subprocess
from urllib.parse import quote
from re import search, I
from charset_normalizer import detect
import configparser, os

# ---------------------------------------------------------------------------
# Configuration — read config.ini from the same directory as this script
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_FILE = os.path.join(_SCRIPT_DIR, 'config.ini')

config = configparser.ConfigParser()
config.read(_CONFIG_FILE)

API_URL = 'https://ws.audioscrobbler.com/2.0/'
API_KEY = config.get('API', 'LASTFM_API_KEY')
API_AD = config.get('API', 'LASTFM_API_AD')
TOKEN_SIG = config.get('API', 'TOKEN_SIG')

# ---------------------------------------------------------------------------
# Paths — read from config.ini [paths] section, with XDG-compliant defaults
# ---------------------------------------------------------------------------
_DATA_DIR = os.path.expanduser(config.get('paths', 'data_dir', fallback='~/.local/share/rs3'))
_CONFIG_DIR = os.path.expanduser('~/.config/rs3')
SHAZAM_LOC = os.path.expanduser(config.get('paths', 'shazam_dir', fallback='~/.config/scripts/rs3_shazam/'))

# Ensure data directory exists
os.makedirs(_DATA_DIR, exist_ok=True)

SCROBBLER_FILE = os.path.join(_DATA_DIR, 'audioscrobbler.queue')
BACKUP_FILE    = os.path.join(_DATA_DIR, 'audioscrobbler_backup.queue')
LOG_FILE       = os.path.join(_DATA_DIR, 'RS3.log')
ERROR_FILE     = os.path.join(_DATA_DIR, 'errors.log')
DEBUG_FILE     = os.path.join(_DATA_DIR, 'debug')
DURATION_FILE  = os.path.join(_DATA_DIR, 'duration.log')
INVALID_FILE   = os.path.join(_DATA_DIR, 'invalid')
SILLY_FILE     = os.path.join(_DATA_DIR, 'silly')
BANNED_FILE    = os.path.expanduser(config.get('paths', 'banned_file', fallback=os.path.join(_SCRIPT_DIR, 'banned_titles')))
def _find_session_file():
    configured = config.get('paths', 'session_file', fallback=None)
    if configured:
        p = os.path.expanduser(configured)
        if not os.path.isabs(p):
            p = os.path.join(_SCRIPT_DIR, p)
        return p
    candidates = [
        os.path.join(_DATA_DIR, '.session'),
        os.path.join(_CONFIG_DIR, '.session'),
        os.path.join(_SCRIPT_DIR, '.session'),
        os.path.expanduser('~/.local/share/rhythmbox/plugins/RS3/.session'),
        '/usr/lib/rhythmbox/plugins/RS3/.session',
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return os.path.join(_DATA_DIR, '.session')

SESSION_FILE   = _find_session_file()

HEADERS = {'User-Agent': 'Mozilla/4.0 (compatible; MSIE 5.5; Windows NT)'}
FILE = {
    'backup': BACKUP_FILE, 'log': LOG_FILE, 'error': ERROR_FILE,
    'debug': DEBUG_FILE, 'duration': DURATION_FILE, 'scrobble': SCROBBLER_FILE,
    'silly': SILLY_FILE, 'session': SESSION_FILE
}
BRAINZ_URL = 'http://musicbrainz.org/ws/1/'
DEF_CAB = {
    'artist': '<artist>(.+)</artist>',
    'title': '<title>(.+)</title>',
    'duration': '<duration>(\\d+)</duration>',
    'album': '<album>(.+)</album>'
}

# ---------------------------------------------------------------------------
# bcolors — terminal colour codes (single canonical definition)
# ---------------------------------------------------------------------------
class bcolors:
    HEADER  = '\033[95m'
    OKBLUE  = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL    = '\033[91m'
    BADBLUE = '\033[96m'
    GREY    = '\033[90m'
    ENDC    = '\033[0m'

    def disable(self):
        self.HEADER = ''
        self.OKBLUE = ''
        self.OKGREEN = ''
        self.WARNING = ''
        self.FAIL = ''
        self.ENDC = ''

# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def record(string, method):				#RS3's own Archival Officer: Archana
  log = open(FILE[method], "a")
  log.writelines(string)
  log.close()

def display(cab, m=0):
  artist = cab['artist']
  title = cab['title']
  album = cab['album']
  track = cab['track']
  duration = cab['duration']
  playcount = cab['playcount']
  listeners = cab['listeners']
  string = '%s - %s - %s - %s %d %d %d\n' % (track, artist, album, title, duration, listeners, playcount)
  return string

def xmllight(string, cab=DEF_CAB):		#Indigenous XML Parser (Serves only RS3)
  for x in cab:
    t = search(str(cab[x]), str(string))
    if t:
      cab[x] = t.group(1)
      if '&' in str(cab[x]):
        cab[x] = html.unescape(str(cab[x]))
    else:
      cab[x] = None

def unescape(text):
    def fixup(m):
        text = m.group(0)
        if text[:2] == "&#":
            # character reference
            try:
                if text[:3] == "&#x":
                    return chr(int(text[3:-1], 16))
                else:
                    return chr(int(text[2:-1]))
            except ValueError:
                pass
        else:
            # named entity
            try:
                text = chr(html.entities.name2codepoint[text[1:-1]])
            except KeyError:
                pass
        return text # leave as is
    return re.sub(r"&#?\w+;", fixup, text)

def is_banned_title(raw_title):				#Checks raw title against banned_titles file (before any splitting)
  """Return True if the raw stream title matches a banned pattern.

  The banned_titles file is re-read on every call so edits are
  picked up live without restarting the daemon.
  Supports plain substring (case-insensitive) and re: prefixed regex.
  """
  if not raw_title:
    return False
  if not os.path.exists(BANNED_FILE):
    return False
  try:
    with open(BANNED_FILE, 'r') as f:
      for line in f:
        line = line.strip()
        if not line or line.startswith('#'):
          continue
        if line.startswith('re:'):
          pattern = line[3:]
          if re.search(pattern, raw_title, re.IGNORECASE):
            return True
        else:
          if line.lower() in raw_title.lower():
            return True
  except Exception as e:
    print(bcolors.WARNING + "Error reading banned_titles: %s" % e + bcolors.ENDC)
  return False

def validator(cab):						#Checks for Invalid Titles
  Relevance = False
  #invalids=open(INVALID_FILE,"r")		#TODO Get a file to store this
  #invalid=invalids.read()
  #invalids.close()				#TODO Instead of 'artist' in array any of array in artist: Culprit - Calm Radio
  if cab['artist'] in ('-', 'VA', 'Various Artists', 'Unknown', 'Unknown Artist', '', None, 'AD BREAK', 'Back', 'ID/PSA', 'Musicplus', 'NONE', 'Radio', 'HBR1.COM', 'The Pulse of Music', 'KEXP.ORG 90.3FM', 'Limited Time Offer', 'DJ Mike Llama', 'Calm Radio', 'STRESSLESS Coupon Offer', ' Relaxation Lives Here', 'Calm Radio - Classical - Relaxation Lives Here.', 'Advert', 'Advert:'):
    Relevance = True
  if cab['title'] in ('-', '', None, 'AD BREAK', 'Radio Gorzow', 'None', 'Broadcasting LIVE', 'bedroom-dj.co.uk', 'SomaFM ID'):
    Relevance = True
  return Relevance

def featify(artist, title):
  featuring = search(r'(.+?)\s+[Ff](?:ea)?t(?:uring)?\.?\s+(.+)', str(artist), I)
  if featuring:
      main_artist = featuring.group(1).strip()
      feat_artist = featuring.group(2).strip()
      bracket = search(r'(.+)\s+(\(.+\))', str(title))
      if bracket:
        title = bracket.group(1).strip() + ' (feat. ' + feat_artist + ') ' + bracket.group(2).strip()
      else:
        title = str(title) + ' (feat. ' + feat_artist + ')'
      return main_artist, title
  return artist, title

# ---------------------------------------------------------------------------
# Last.fm API functions
# ---------------------------------------------------------------------------

def get_correction(artist, title):
  url = API_URL + "?method=track.correction&artist=%s&track=%s&api_key=%s" % (quote(artist), quote(title), API_KEY)
  cab = {'artist': '<artist>\n    <name>(.+)</name>', 'title': '<track>\n    <name>(.+)</name>'}
  #url=info_url.replace(' ','+')
  #url=nurl.encode('utf-8')			#trying Luck
  #print ("Corrector: "+str(url))			#Debug
  print(bcolors.OKBLUE + "Attempting Correction on " + artist + " -- " + title + bcolors.ENDC)
  try:
    page = urllib.request.urlopen(url)
  except urllib.error.URLError as e:
    print(bcolors.BADBLUE + "Correction on " + artist + " - " + title + " : " + e.reason + bcolors.ENDC)
    return artist, title
  try:
    doc = page.read()
  except:
    print("no correction page")
    return artist, title
  #if search('artistcorrected=.(\d). trackcorrected=.(\d).',str(doc)):	#Major Hacks Ahead
  if search('artistcorrected', str(doc)):			#1
    xmllight(doc.decode('utf-8'), cab)			#'utf-8'%'unicode-escape' will let you escape from unicode hell
    print("Correctered!")	#2
    return cab['artist'], cab['title']
  return artist, title

def get_fm(fab):					#Fix this HACK
  artist = fab['artist']
  title = fab['title']
  values = {'artist': artist, 'track': title}
  data = urllib.parse.urlencode(values)
  url = API_URL + "?method=track.getinfo&" + data + "&api_key=%s" % API_KEY
  try:
    info_page = urllib.request.urlopen(url)
  except urllib.error.URLError as e:
    print(bcolors.FAIL + "Fetching of " + artist + " - " + title + " : " + e.reason + bcolors.ENDC)
    #print (url)
    return 0, None
  if info_page.getcode() == 200:
    doc = info_page.read()
    cab = {'listeners': '<listeners>(\\d+)</listeners>', 'album': '<title>(.+)</title>', 'duration': '<duration>(\\d+)</duration>', 'playcount': '<playcount>(\\d+)</playcount>', 'year': '<published>\\d\\d [A-Za-z]{3} (\\d{4}).+</published>'}
    xmllight(doc.decode('utf-8'), cab)				#'utf-8'%'unicode-escape' will let you escape from unicode hell
    try:
        cab['duration'] = duration = int(int(cab['duration']) / 1000)
    except:
        cab['duration'] = duration = 0
    try:
        cab['listeners'] = listeners = int(cab['listeners'])
    except:
        cab['listeners'] = listeners = 0
    try:
        cab['playcount'] = playcount = int(cab['playcount'])
    except:
        cab['playcount'] = playcount = 0
    if cab['album']:
      print(bcolors.OKBLUE + "Album available: %s" % str(cab['album']) + bcolors.ENDC)
      if fab['album']:
        if str(fab['album']).upper != str(cab['album']).upper:
          print("Provided album is %s but last fm says %s" % (str(fab['album']), str(cab['album'])))
      return 1, cab
    #relevant=fab['rotprob']
    relevant = 0			#HaXXED
    if (duration in (0, 30, 666)) or relevant or listeners < 5 or (playcount < 10 and "ix)" not in title):
      print(bcolors.BADBLUE + "Needs more cowbell %s/%s for %s sec" % (str(cab['playcount']), str(cab['listeners']), str(cab['duration'])) + bcolors.ENDC)
      string = '%s - %s %s\n' % (artist, title, str(cab))
      record(string, 'duration')
      return 0, cab
    return 1, cab
  else:
    print(bcolors.FAIL + "Access unsuccessful for url:" + url + " with error: %d" % info_page.getcode() + bcolors.ENDC + '\n')	#Legacy code from Python 2 Era
    return 0, None


def straighten(artist, title):			#Parlour tricks get things done TODO  Throw Test Cases
  Relevant = False
  if ('_' in artist) or ('_' in title):
    artist = artist.replace('_', ' ')
    title = title.replace('_', ' ')
    Relevant = True
  if ('`' in artist) or ('`' in title):
    artist = artist.replace("`", "'")
    title = title.replace("`", "'")
    Relevant = True
  if ',' in (artist):
    print(bcolors.WARNING + "Contains comma" + bcolors.ENDC)
    temp = artist.partition(', ')
    artist = temp[2] + ' ' + temp[0]
    Relevant = True
  if ('`' in artist) or ('`' in title):
    artist = artist.replace("`", "'")
    title = title.replace("`", "'")
    Relevant = True
  if ("\\'" in artist) or ("\\'" in title):
    artist = artist.replace("\\'", "'")
    title = title.replace("\\'", "'")
    Relevant = True
  if (':' in title):
    citle = title.partition(': ')
    title = citle[2]
    Relevant = True
  if ("&amp;" in artist) or ("&amp;" in title):
    artist = artist.replace("&amp;", '&')
    title = title.replace("&amp;", '&')
    Relevant = True
  return Relevant, artist, title

def scrobble(artist, title, album, track, duration, ptime, session_key):
  if not session_key:
    print(bcolors.FAIL + "Scrobble skipped: No session_key" + bcolors.ENDC)
    return
  parameters = {'api_key': API_KEY, 'method': "track.scrobble", 'sk': session_key, 'artist': artist, 'track': title, 'timestamp': ptime, 'duration': duration}
  if album:
    parameters['album'] = album
  if track:
    parameters['trackNumber'] = track
  parameters = get_sig(parameters)
  data = urllib.parse.urlencode(parameters)
  bdata = data.encode('utf-8')
  req = urllib.request.Request(API_URL, bdata)
  try:
    resp = urllib.request.urlopen(req).read()
    if b'ok' in resp:
      print(bcolors.OKGREEN + "Scrobbled: %s - %s" % (artist, title) + bcolors.ENDC)
    else:
      print(bcolors.FAIL + "Scrobble response: %s" % resp.decode('utf-8', errors='ignore') + bcolors.ENDC)
  except urllib.error.HTTPError as e:
    err_body = e.read().decode('utf-8', errors='ignore')
    print(bcolors.FAIL + "Scrobble HTTP error %d: %s" % (e.code, err_body) + bcolors.ENDC)
  except Exception as e:
    print(bcolors.FAIL + "Scrobble error: %s" % str(e) + bcolors.ENDC)


def update_nowplaying(artist, title, session_key, reprint=1):
  if not session_key:
    if reprint:
      print(bcolors.WARNING + "NowPlaying skipped: No session_key" + bcolors.ENDC)
    return
  parameters = {'api_key': API_KEY, 'method': "track.updateNowPlaying", 'sk': session_key, 'artist': artist, 'track': title}
  parameters = get_sig(parameters)
  data = urllib.parse.urlencode(parameters)
  bdata = data.encode('utf-8')
  req = urllib.request.Request(API_URL, bdata)
  try:
    resp = urllib.request.urlopen(req).read()
    if b"ok" in resp and reprint:
      print(bcolors.OKBLUE + "Now Playing: %s - %s" % (artist, title) + bcolors.ENDC)
    elif b"ok" not in resp:
      print(bcolors.WARNING + "NowPlaying response: %s" % resp.decode('utf-8', errors='ignore') + bcolors.ENDC)
  except urllib.error.HTTPError as e:
    err_body = e.read().decode('utf-8', errors='ignore')
    print(bcolors.WARNING + "NowPlaying HTTP error %d: %s" % (e.code, err_body) + bcolors.ENDC)
  except Exception as e:
    print(bcolors.WARNING + "NowPlaying error: %s" % str(e) + bcolors.ENDC)


def get_subscriber():
  try:
    with open(SESSION_FILE, "r") as stream:
      subscriber = search(r'subscriber\s*=\s*(\d)', stream.read()).group(1)
      return int(subscriber)
  except:
    return 1

def getsession():
  try:
    with open(SESSION_FILE, "r") as stream:
      sk = search(r'session_key\s*=\s*(.+)', stream.read()).group(1).strip()
      return sk
  except:
    parameters = {'api_key': API_KEY, 'method': "auth.getToken"}
    param = get_sig(parameters)
    doc = get_doc(param)
    if not doc:
      return None
    cab = {'token': '<token>(.+)</token>'}
    xmllight(doc, cab)
    if not cab['token']:
      print("Error getting token")
      return None
    authurl = 'http://www.last.fm/api/auth/?api_key=%s&token=%s' % (API_KEY, str(cab['token']))
    print(authurl)
    try:
      webbrowser.open(authurl, new=2, autoraise=True)
    except:
      print("Error opening the auth url")
      return None
    #dialog = gtk.Dialog("Acceptance", None, 0,(gtk.STOCK_OK, gtk.RESPONSE_ACCEPT))
    #label=gtk.Label("Click OK once you grant permission for radioscrobbler")
    #label.show()
    #dialog.get_content_area().add(label)
    #response = dialog.run()
    #dialog.destroy()
    #while gtk.events_pending():
      #gtk.main_iteration(False)
    #if response == gtk.RESPONSE_ACCEPT:
      #print "Yeah"
    #else:
      #return None
    #p=input("Press Enter once you grant permission")
    time.sleep(20)
    print("Waited 20 seconds")
    parameters = {"method": "auth.getsession", "api_key": API_KEY, "token": str(cab['token'])}
    param = get_sig(parameters)
    doc = get_doc(param)
    if not doc:
      print("Couldn't get session key")
      return None
    cab = {'name': '<name>(.+)</name>', 'sk': '<key>(.+)</key>', 'subscriber': '<subscriber>(\\d)</subscriber>'}
    xmllight(doc, cab)
    string = "[radioscrobbler]\nuser=%s\nsubscriber=%s\nsession_key=%s" % (cab['name'], cab['subscriber'], cab['sk'])
    record(string, 'session')
    sk = cab['sk']
  return sk


def get_sig(parameters):
    md5 = hashlib.md5()
    keys = sorted(parameters.keys())
    keyvalues = []
    for key in keys:
        keyvalues.append(str(key))
        keyvalues.append(str(parameters[key]))

    sig = ''.join(keyvalues)
    sig = sig + API_AD
    #md5.update(sig.encode("utf-8"))
    try:
      sig = sig.encode('utf-8')
    except UnicodeDecodeError:
      print("Romeo died!")
    md5.update(sig)
    api_sig = md5.hexdigest()
    parameters["api_sig"] = api_sig
    return parameters

def get_doc(parameters):
    data = urllib.parse.urlencode(parameters)
    url = "%s?%s" % (API_URL, data)
    try:
      return urllib.request.urlopen(url).read()
    except:
      print("Sorry")
      return None

def get_prob(artist, title):					#Tiny thought this would help weed out false negatives in "Disabled by Last FM"
  if search('(-|\\d{4}|\\(|\\))', artist) or search('(-|\\d{4}|\\(|\\))', title):
    return True
  return False

def get_rec():
	path = "/tmp/alsa_record.wav"
	python3_rommand = "arecord -D pulse -c 2 -f S32_LE -r 48000 -d 5 " + path
	#rec_proc = subprocess.Popen(python3_rommand.split())
	#return path
	rec_proc = subprocess.Popen(python3_rommand.split(), stdout=subprocess.PIPE)
	output, error = rec_proc.communicate()
	if error:
		print("Recording Error:" + error)
		return None
	else:
		return path

def get_shazam(fab, path):
	print("Shazammed")
	loc = SHAZAM_LOC
	python3_command = "python3 " + loc + "identify_sound.py -s " + path + " -c " + loc + "shazam_on_linux.conf"  # launch your python3 script using bash
	shaz_proc = subprocess.Popen(python3_command.split(), stdout=subprocess.PIPE)
	output, error = shaz_proc.communicate()  # receive output from the python2 script
	if error:
		print("Shazam Script Error:" + error)
		return 0, fab
	else:
		output = output.decode("utf-8")
		data = search("Track : (.+)\nArtist : (.+)\nAlbum : (.+)\nLabel : (.+)\nDuration : (.+)\nRelease : (.+)", output)
		if data:
			fab['title'] = data.group(1)
			fab['artist'] = data.group(2)
			fab['album'] = data.group(3)
			fab['label'] = data.group(4)
			fab['duration'] = data.group(5)
			fab['release'] = data.group(6)
			return 1, fab
		else:
			print("Track couldn't be identified")
			return 0, fab
