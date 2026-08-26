"""
rs3_core.py — RadioScrobbler3 Core Engine

Scrobbling logic extracted from the original RS3 Rhythmbox plugin.
All Rhythmbox/libpeas dependencies removed. Receives track metadata
from rs3_daemon.py (via MPRIS D-Bus) and handles:
  - Title splitting (simple, pattern, preset, shazam)
  - Last.fm lookups, corrections, scrobbling
  - Encoding detection and fixup
  - Webpage scraping for stations that need it
  - Delayed scrobbling for uncertain tracks
"""

from re import search, escape, sub
from urllib.parse import urlencode
from charset_normalizer import detect
from html import unescape
import time, S3, _thread, urllib.request, sys, os
from S3 import bcolors

# ---------------------------------------------------------------------------
# Paths — read from config.ini via S3 module's config object
# ---------------------------------------------------------------------------
_CONFIG_DIR = os.path.expanduser('~/.config/rs3')
PATTERN_FILE = os.path.expanduser(S3.config.get('paths', 'patterns_file', fallback=os.path.join(_CONFIG_DIR, 'patterns')))
RULE_FILE    = os.path.expanduser(S3.config.get('paths', 'rules_file',    fallback=os.path.join(_CONFIG_DIR, 'rules')))

HEADERS = {'User-Agent': 'Mozilla/4.0 (compatible; MSIE 5.5; Windows NT)'}
DECODER = {'a': 'artist', 'b': 'album', 't': 'title', 'n': 'track'}
DEBUG = int(S3.config.get('daemon', 'debug', fallback='0'))


class RS3Core:
    """
    The scrobbling engine. Instantiated by rs3_daemon.py.

    The daemon provides a `is_playing_fn` callback so the core can check
    whether the player is still active (replaces the old
    self.player.get_playing()[1] calls).
    """

    def __init__(self, is_playing_fn):
        """
        Args:
            is_playing_fn: callable that returns True if the player is
                           currently playing (queried via D-Bus by the daemon).
        """
        self.is_playing = is_playing_fn
        self.elapsed = None
        self.startime = None
        self.old_title = None
        self.current_location = None
        self.current_radio = None
        self.current_title = None
        self.data = None
        self.override = 0
        self.scrobble = 0
        self.shaztm = 0
        self.shaddy = 0
        self.first_play = 1
        self.minveto = 1
        self.valid = 1
        self.ordering = None
        self.pattern = None
        # Station flags (previously from Rhythmbox Genre field, now from rules)
        self.station_flags = set()  # e.g. {'shaz', 'naz', 'd0s3'}

    # -----------------------------------------------------------------------
    # Signal handlers — called by the daemon
    # -----------------------------------------------------------------------

    def on_playing(self):
        """Called when playback starts or resumes."""
        print("Playing")
        if self.shaddy == 0:
            self.delayscrobble()
        self.shaddy = 0

        if 'shaz' in self.station_flags:
            self.data = None
            self.shazam()
            if self.data:
                self.current_title = "%s - %s" % (self.data['artist'], self.data['title'])
                duration = self.data['duration']
            else:
                self.current_title = None
                duration = 300
            if self.old_title != self.current_title:
                self.set_time()
                logstring = "%s\t%s\t%s\t%s\n" % (self.current_title, str(self.startime), str(self.elapsed), self.current_radio)
                S3.record(logstring, 'log')
                if self.current_title and self.valid:
                    S3.record(S3.display(self.data), 'silly')
                    self.scrobbler()
                self.valid = 1
            self.shaztm += 1
            try:
                _thread.start_new_thread(self.shazam_timer, (duration,))
            except:
                print(bcolors.FAIL + "Error: unable to start thread " + str(sys.exc_info()[0]) + bcolors.ENDC)
            self.old_title = self.current_title

    def on_stopped(self):
        """Called when playback stops."""
        print("Not Playing")

    def on_station_changed(self, location, station_name):
        """
        Called when a new radio station starts playing.

        Args:
            location: stream URL
            station_name: human-readable station name
        """
        if location == self.current_location:
            return  # same station, nothing to do
        self.current_location = location
        self.current_radio = station_name
        print(bcolors.HEADER + "Radio: %s Location: %s" % (self.current_radio, self.current_location) + bcolors.ENDC)
        self.rule_check()

    def on_title_changed(self, new_title):
        """
        Called when the stream title changes (new song on same station).

        Args:
            new_title: the new stream title string
        """
        self.current_title = new_title
        if not self.override == 1:
            self.set_status()
        else:
            self.old_title = new_title

    # -----------------------------------------------------------------------
    # Core engine
    # -----------------------------------------------------------------------

    def set_status(self):				#Sets status from current title
        if self.old_title != self.current_title and self.current_title:   #Make sure track name changed and it is not Null
            self.current_title = self.current_title.replace(u'\ufeff', '') 	#Ugly hack
            print(bcolors.HEADER + "♫ %s ♫ " % (self.current_title) + bcolors.ENDC)
            if self.override != 2:
                self.core()
                self.old_title = self.current_title		#Sets current title as old title

    def shazam_timer(self, duration):
        switch = self.shaztm
        if duration == 0:
            duration = self.shaz_dur
            self.shaddy = 1
            print("No DelayScrobble for this track")
        print(bcolors.HEADER + "Timer(%d) set for %d secs" % (switch, duration) + bcolors.ENDC)
        time.sleep(int(duration) - 10)
        if switch == self.shaztm:
            self.shaztm = 0
            if self.is_playing():
                self.on_playing()

    def shazam(self):
        cab = {'artist': None, 'title': None, 'album': None, 'track': None}
        if 'naz' in self.station_flags:
            return
        path = S3.get_rec()
        if path:
            success, cab = S3.get_shazam(cab, path)
            if success:
                print(bcolors.HEADER + "♫ %s - %s ♫ " % (cab['artist'], cab['title']) + bcolors.ENDC)
                self.shaz_dur = int(int(cab['duration']) / 1000)
                year = cab['release'][:4]
                if 'None' in year:
                    bracket = ""
                else:
                    bracket = '(' + year + ') '
                print(bcolors.GREY + "♫ Label: %s | Album: %s %s| %d secs ♫" % (cab['label'], cab['album'], bracket, self.shaz_dur) + bcolors.ENDC)
                success2, fab = self.get_duration(cab)
                if fab == None:		#CHECKKKK
                    self.valid = 0
                    cab['duration'] = self.shaz_dur
                    self.data = cab
                    return
                if fab['duration'] == None:
                    fab['duration'] = 0
                try:
                    opc = self.data['playcount']
                except:
                    opc = 0
                if (success2 == 1) or (fab['playcount'] >= opc) or (fab['duration'] > 30):
                    if (fab['duration'] == 0) and (fab['playcount'] > 5):
                        fab['duration'] = self.shaz_dur
                    self.data = fab
                else:
                    print("Shazam entry isn't popular enough")
                    if self.data['album'] == None:
                        self.data['album'] = cab['album']			#duration as well

    def core(self):						#Core of the Plugin, the Engine if you may..
        if S3.is_banned_title(self.current_title):
            print(bcolors.GREY + "Banned title, skipping: %s" % self.current_title + bcolors.ENDC)
            return
        if self.shaztm:
            if 'shaz' in self.station_flags:
                return
        self.delayscrobble()
        self.set_time()
        logstring = "%s\t%s\t%s\t%s\n" % (self.current_title, str(self.startime), str(self.elapsed), self.current_radio)
        S3.record(logstring, 'log')
        self.enc_check()		#Check for Encoding Error
        self.veto_check()
        success = 0
        if self.override == 4:
            print(bcolors.GREY + "Preset split %s" % self.current_title + bcolors.ENDC)
            success = self.presetsplit()
        elif not self.override:
            #proposed site for Validate
            print(bcolors.GREY + "Simplesplit" + bcolors.ENDC)
            success = self.simplesplit()
            if not success:
                success = self.patternsplit()
        if not success:
            self.shazam()
        if self.data:
            try:
                S3.record(S3.display(self.data), 'silly')
                #print ("Thou shall be scrobbled")			#Debug
                self.scrobbler()
            except:
                print("Unable to Scrobble -> Stream could be pushing junk title")

    def set_time(self):						#Sets the time (duh!)
        if self.startime != None:
            self.elapsed = int(time.time()) - self.startime
        self.startime = int(time.time())
        print(bcolors.OKBLUE + "Previous track lasted for: %s" % str(self.elapsed) + " secs" + bcolors.ENDC)

    def simplesplit(self):					#The simplest way :)
        self.data = Relevant = None
        partition = self.current_title.partition(' - ')
        artist = partition[0]
        title = partition[2]
        if artist == '' or title == '':
            print("No title")
            return 0
        cab = {'artist': artist, 'title': title, 'album': None, 'track': None}
        success, self.data = self.get_duration(cab)
        #print(self.data)
        if not success:
            #print("No Success in Simple, going for correction")
            success = self.corrector(artist, title)
        return success				#HAXXED

    def get_duration(self, cab, uv=0):			#Get Duration et .al of track from last fm
        cab['artist'], cab['title'] = S3.featify(cab['artist'], cab['title'])
        if S3.validator(cab):
            print(bcolors.FAIL + "Invalid" + bcolors.ENDC)
            return 0, None
        relevant = cab['rotprob'] = S3.get_prob(cab['artist'], cab['title'])
        #relevant = 1 #HAXX
        success, fab = S3.get_fm(cab)
        if success:
            print(bcolors.OKGREEN + "Success" + bcolors.ENDC)
            print(fab)
        if fab:
            if (fab['duration'] in (30, 666)) and relevant:
                print(bcolors.WARNING + "Disabled by last fm: " + str(fab['duration']) + bcolors.ENDC)
                return 0, None
            fab['artist'] = cab['artist']
            fab['title'] = cab['title']
            fab['track'] = cab['track']
            if not fab['album']:
                fab['album'] = cab['album']

            if int(fab['listeners']) != 0:
                #fab['ratio']=int(int(fab['playcount'])/int(fab['listeners']))
                ratio = str(int(fab['playcount']) / int(fab['listeners']))
                fab['ratio'] = ratio[:4]
        return success, fab

    def enc_check(self):	#Check the encoding and correct if necessary
        try:
            k = detect(self.current_title.encode('windows-1252'))
        except:
            print(bcolors.WARNING + "Backfired :", sys.exc_info()[0], bcolors.ENDC)
            return
        #k=detect(self.current_title.encode('windows-1252'))
        if not (k['encoding'] == 'ascii'):
            debugstring = "%s : %s\t%s\n" % (self.current_title, k['encoding'], self.current_radio)
            S3.record(debugstring, 'debug')
            if k['encoding'] in ('windows-1251', 'ISO-8859-8', 'MacCyrillic', 'KOI8-R'):
                self.current_title = self.current_title.encode('windows-1252').decode('windows-1251')
                print(bcolors.OKBLUE + "Changing the encoding - " + self.current_title + bcolors.ENDC)
            elif k['encoding'] == 'ISO-8859-2':
                print(self.current_title.encode('windows-1252').decode('ISO -8859-2'))
            else:
                print(bcolors.OKBLUE + "The detected encoding is %s" % k['encoding'] + bcolors.ENDC)

    def rule_check(self):
        self.override = 0
        self.station_flags = set()
        #if search('/play\?s=([^&]+)',self.current_location):
            #self.live365()
        if not os.path.exists(RULE_FILE):
            return
        rules = open(RULE_FILE, "r")
        rule = rules.read()
        rules.close()
        found = search(escape(self.current_location) + r"\t([ynw])([hxlsiabtn]{1,4})#'(.+)'#(?:[\t,](.+))?", rule)
        if found:
            # Parse station flags from optional 4th column (e.g. shaz, naz, d0s3)
            if found.group(5):
                for flag in found.group(5).strip().split(','):
                    flag = flag.strip()
                    if flag:
                        self.station_flags.add(flag)

            if found.group(1) == 'n':
                self.override = 1
                print("BANNED!")
            if found.group(1) == 'w':
                self.override = 2
                print("Webbed and..")
                self.first_play = 1
                if found.group(2) == 'h':
                    print("Paged")
                    string = found.group(3)
                    self.paged(string)
                if found.group(2) == 'l':
                    print("last fm")
                    user = found.group(3)
                    #self.last_fm(user)
                if found.group(2) == 'a':
                    print("24 hour classical public radio")
                    #self.public_radio()
                if found.group(2) == 't':
                    print("czech radio")
                    #self.czech_radio()
                if found.group(2) == 'i':
                    print("icecast")
                    data = found.group(3)
                    #self.ice(data)
                if found.group(2) == 'n':
                    data = found.group(3)
                    #self.jsonet(data)
            if found.group(1) == 'y':
                self.override = 4
                self.ordering = self.pattern = None
                if 'a' and 't' in str(found.group(2)):
                    self.ordering = found.group(2)
                    self.pattern = found.group(3)

    def presetsplit(self):						#Splits according to your explicit command for a specific station
        self.data = None
        if self.ordering and self.pattern:
            t = search(self.pattern, self.current_title)
            if t:
                success, self.data = self.get_tag(t, self.ordering, 1)
                return success
            else:
                print(bcolors.FAIL + "No pattern match" + bcolors.ENDC)

    def get_tag(self, data, ordering, surity=0):		#Title+Pattern+Order = Artist+Title+Album (Boom Shankar)
        cab = {'artist': None, 'title': None, 'track': None, 'album': None}
        n, Relevant = 0, False
        while n < len(ordering):
            cab[DECODER[ordering[n]]] = data.group(n + 1)
            n += 1
        if not cab['title']:
            print(bcolors.FAIL + "No title found after presetsplit, Must be banned.." + bcolors.ENDC)
            return 0, None
        print(cab)
        success, fab = self.get_duration(cab, surity)
        if not success:
            artist, title, album = cab['artist'], cab['title'], cab['album']
            Relevant, artist, title = S3.straighten(artist, title)			#Remove minor imperfections
            if Relevant:
                cab['artist'] = artist
                cab['title'] = title
                success, self.data = self.get_duration(cab)
        if not success:												#HAXXED
            success = self.corrector(artist, title)
            #print ("The corrector below straighten is done")
        return success, fab

    def corrector(self, artist=None, title=None, album=None):		#Correct minor spelling mistakes
        if (not self.data) and (not artist):
            print("Ousted at the onset")
            return 0
        if self.data and (not artist):
            artist, title, album = self.data['artist'], self.data['title'], self.data['album']
        if self.data or artist:
            kartist, ktitle = S3.get_correction(artist, title)			#HAXX Galore
            #if artist!=str(self.data['artist']) or title!=str(self.data['title']):
            if kartist != artist or ktitle != title:
                #cab={'artist':kartist,'title':ktitle,'album':self.data['album'],'track':None}		#HAXX
                cab = {'artist': kartist, 'title': ktitle, 'album': None, 'track': None}		#HAXX
                success, mab = self.get_duration(cab)
                if success or mab:
                    self.data = mab
                    return success


    def patternsplit(self):						#Pattern Splitter, the magician
        BlackFlag = WhiteFlag = None
        if not os.path.exists(PATTERN_FILE):
            print(bcolors.WARNING + "Pattern file not found: %s" % PATTERN_FILE + bcolors.ENDC)
            return 0
        pattern_list = open(PATTERN_FILE, "r")
        while (WhiteFlag == None):
            pattern = pattern_list.readline()
            if pattern == '':
                WhiteFlag = 1
            pat = search(r"'(.+)'\t([cabntx]{1,4})#<eg>(.+)", pattern)
            if (pat):
                match_regex = pat.group(1)
                ordering = pat.group(2)
                example = pat.group(3)
                BlackFlag = WhiteFlag = search(match_regex, self.current_title)
        pattern_list.close()
        if BlackFlag == None:
            print(bcolors.BADBLUE + "No matching expression!!! Go to website or get the regular expression for %s" % self.current_title + bcolors.ENDC)
            #self.current_title=None
            return 0
        else:
            print(bcolors.OKBLUE + "Matched expression for %s" % example + bcolors.ENDC)
            if 'c' in ordering:					#The Villain who deals with Freakin_Capitalists
                ordering = sub('c', '', ordering)
                print("c in ordering!")
                mark = str(self.current_title)
                shark = sub(r"([a-zA-Z0-9])([A-Z])", r"\1 \2", mark)
                self.current_title = sub(r"_", r"_-_", str(shark), 1)
                self.patternsplit()
            if 'x' in ordering:
                success, cab = self.AAAsplit(BlackFlag, ordering)
                #print("Haxxed Xsplit")				#Haxxed
            else:
                success, cab = self.get_tag(BlackFlag, ordering)
            if cab:
                if self.data:
                    if self.data['playcount'] < cab['playcount']:
                        self.data = cab
                    return success
                else:
                    self.data = cab
                return success
            return 0

    def AAAsplit(self, m0, ordering):				#Split BAAAD ones
        if ordering.count('x') == 3:
            success, orcer = [0, 0, 0], ['abt', 'atb', 'tba']
            cab = [None, None, None]
            fab = {'playcount': 0}
            if self.data:
                if self.data['playcount']:
                    fab = {'playcount': self.data['playcount']}
            else:
                fab = {'playcount': -1}
            n = 0
            while n < 3:
                success[n], cab[n] = self.get_tag(m0, orcer[n])
                if success[n]:
                    return success[n], cab[n]
                elif cab[n]:
                    if cab[n]['playcount'] > fab['playcount']:
                        fab = cab[n]
                n += 1
            if fab['playcount'] == -1:
                return 0, None
            else:
                return 0, fab


    def paged(self, string, title=None):    				#Gets stream title info from webpages. Ugly Beast
        data = search(r"'(.+)'<\[([atbdr]{2,5})]>'(.+)'", string)
        location = self.current_location
        cab, refresh = {'rotprob': 0, 'artist': None, 'title': None, 'album': None, 'refresh': None, 'duration': None, 'playcount': 1, 'track': None}, None
        if data:
            url = data.group(1)
            order = data.group(2)
            pattern = data.group(3)
            if "0.36441301471309195" in url:                            #BIG F***ing HACK!! for Radio Grischa..
                import random
                randomvalue = random.random()
                url = url.replace("0.36441301471309195", str(randomvalue))
            req = urllib.request.Request(url, None, HEADERS)
            try:
                doc = urllib.request.urlopen(req).read()
                if DEBUG:
                    print(doc[:20])
                #print doc
                #print pattern
            except:
                print("Playlist data unavailable")
                return
            #t=search(pattern,doc.decode('utf-8'))
            t = search(pattern, doc.decode('unicode-escape'))		#'utf-8'%'unicode-escape' will let you escape from unicode hell
            if t:
                cab['title'] = t.group(order.index('t') + 1)
                if '&' in str(cab['title']):
                    cab['title'] = unescape(str(cab['title']))
                #print("Check %s - %s"%(str(cab['title']),title))		#Check
                if title != str(cab['title']):
                    self.delayscrobble()
                    self.set_time()
                    self.veto_check()
                    if title != None:
                        self.first_play = 0
                    cab['artist'] = t.group(order.index('a') + 1)
                    if '&' in str(cab['artist']):
                        cab['artist'] = unescape(str(cab['artist']))
                    print("%s - %s" % (str(cab['artist']), str(cab['title'])))
                    logstring = "%s - %s\t%s\t%s\t%s" % (str(cab['artist']), str(cab['title']), str(self.startime), str(self.elapsed), self.current_radio) + '\n'
                    S3.record(logstring, 'log')
                    if 'b' in order:
                        cab['album'] = t.group(order.index('b') + 1)
                    if 'd' in order:
                        cab['duration'] = t.group(order.index('d') + 1)
                        if ':' in str(cab['duration']):
                            partition = str(cab['duration']).partition(':')
                            cab['duration'] = int(partition[0]) * 60 + int(partition[2])
                        elif cab['duration'] == '':
                            refresh = 5
                            cab['duration'] = 0
                        elif int(cab['duration']) > 5000:
                            cab['duration'] = int(cab['duration']) / 1000
                    if 'r' in order:
                        refresh = t.group(order.index('r') + 1)
                        if ':' in refresh:
                            partition = refresh.partition(':')
                            refresh = int(partition[0]) * 60 + int(partition[2])
                        else:
                            refresh = int(t.group(order.index('r') + 1))
                    #print(cab)
                    if cab['title'] and not cab['duration']:
                        success, fab = self.get_duration(cab)
                        if success == 0:
                            self.shazam()
                        try:
                            #print(fab['duration']);print(cab['duration'])
                            if fab['duration']:
                                cab['duration'] = fab['duration']
                        except:
                            print("Bad Title")
                        refresh = 5
                    if (not refresh) and (not self.first_play) and cab['duration']:
                        refresh = cab['duration'] - 1
                        print("No refresh no firstplay and duration exists")

                    if not refresh:
                        refresh = 5
                    if cab['duration'] == None:
                        print(cab)
                        cab['duration'] = 0
                    if int(cab['duration']) in (30, 666, 3966):
                        print("Disabled by last fm")
                    else:
                        if not S3.validator(fab):
                            try:
                                selfplay = int(self.data['playcount'])
                            except:
                                selfplay = 0
                            try:
                                fabplay = int(fab['playcount'])
                            except:
                                fabplay = 0
                            if (success == 1) or (selfplay <= fabplay):
                                if success == 0:
                                    print("self.data==fab")
                                self.data = fab
                            self.scrobbler()
                def next_song(location, refresh, string, title):
                    time.sleep(int(refresh))
                    if location == self.current_location and self.is_playing():
                        self.paged(string, title)
                if not refresh:
                    refresh = 5
                if DEBUG:
                    print(refresh)
                try:
                    _thread.start_new_thread(next_song, (location, refresh, string, str(t.group(order.index('t') + 1))))
                except:
                    print("Error: unable to start thread")

            else:
                print("no match in page")
                self.delayscrobble()
                self.data = None
                self.shazam()
                if self.data:
                    S3.record(S3.display(self.data), 'silly')
                    self.scrobbler()
        else:
            print("Pattern Matching problems (Bazinga!)")

    def veto_check(self):
        # Check station name for d0s flag
        veto = search('d0s([0-9!])', str(self.current_radio))
        if not veto:
            # Also check station_flags from rules file (e.g. 'd0s3')
            for flag in self.station_flags:
                m = search('^d0s([0-9!])$', flag)
                if m:
                    veto = m
                    break
        if veto:
            print("Veto Set to Non-Zero")
            if veto[1] == '!':
                self.minveto = 99999
            else:
                self.minveto = int(veto[1])
        else:
            self.minveto = 1


    def delayscrobble(self):				#When we are unsure of a track, we delayscrobble it.
        if self.scrobble:
            self.scrobble = 0
            if self.data:			#Maybe this check will solve the None error
                self.data['duration'] = int(time.time()) - self.startime
                if (self.data['duration'] > 40) and (self.data['playcount'] >= self.minveto):
                    self.scrob_write()


    def scrobbler(self):
        def real_scrobbler(tyme, location, refresh, title):
            factor = int(refresh / 15)
            while factor > 0:
                time.sleep(15)
                try:
                    S3.update_nowplaying(self.data['artist'], self.data['title'], S3.getsession(), 0)
                except:
                    pass
                factor -= 1
            if (tyme == self.startime) and (location == self.current_location) and self.is_playing():
                self.scrob_write()
            else:
                print(bcolors.FAIL + "Rejected at dusk: %s" % title + bcolors.ENDC)
        if self.data['duration'] in (0, 30, 31, 666) and not self.shaddy:
            print(bcolors.BADBLUE + "Later.." + bcolors.ENDC)
            self.scrobble = 1
        elif self.data['duration'] < 30 or self.data['playcount'] < self.minveto:
            print(bcolors.WARNING + "Rejected at dawn: %s" % str(self.data['title']) + bcolors.ENDC)
        else:
            S3.update_nowplaying(self.data['artist'], self.data['title'], S3.getsession())
            refresh = min(self.data['duration'] / 2, 240)
            cur_title = self.data['title']
            try:
                _thread.start_new_thread(real_scrobbler, (self.startime, self.current_location, int(refresh), cur_title,))
            except:
                print(bcolors.FAIL + "Error: unable to start thread " + str(sys.exc_info()[0]) + bcolors.ENDC)


    def scrob_write(self):
        try:
                subm_list = [('a', self.data['artist']), ('t', self.data['title']), ('l', self.data['duration']), ('i', int(time.time()))]
        except:
                print(bcolors.FAIL + "Can't scrobble" + bcolors.ENDC)
                return
        if self.data['album']:
                subm_list.append(('b', self.data['album']))
        if self.data['track']:
                subm_list.append(('n', self.data['track']))
        S3.scrobble(self.data['artist'], self.data['title'], self.data['album'], self.data['track'], self.data['duration'], self.startime, S3.getsession())
        subm_string = urlencode(subm_list, 1)
        subm_string = subm_string.replace('+', '%20')
        subm_string = subm_string + '\n'
        print(bcolors.OKGREEN + "writing to queue %s" % subm_string + bcolors.ENDC)
        #S3.record(subm_string,'scrobble')
        S3.record(subm_string, 'backup')
