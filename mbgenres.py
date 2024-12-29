from beets import plugins, ui, dbcore
import time
import datetime
import requests
import json
import os
from operator import itemgetter
from itertools import groupby

WHITELISTFILE = os.path.join(os.path.dirname(__file__), "tagwhitelist.txt")
VERSION = "1.0"

class MBGenresPlugin(plugins.BeetsPlugin):

    # Stores the date an item's genre was last updated
    item_types = {"genreupdated": dbcore.types.STRING}
    @property
    def album_types(self):
        return {"genreupdated": dbcore.types.STRING}
    
    # Stores the genre whitelist
    whitelist = [];

    def __init__(self):
        super().__init__()

        self.config.add(
            {
                "genrecount": 5,
                "minvotes": 1,
                "fallback": None,
                "dynamiccount": True,
                "dynamicdivider": 2,
                "artistfallback": True,
                "replace": False,
                "auto": False,
                "separator": ";",
                "titlecase": False,
                "updatefrequency": 7,
            }
        )

        # Add genre tagging to import if configured
        if self.config["auto"]:
            self.import_stages = [self.mbgenresImport]
        
        # Read genres from whitelist file, creating the file if it does not exist
        try:
            with open(WHITELISTFILE, "r") as f:
                for line in f:
                    self.whitelist.append(line.strip())
        except:
            self._log.debug("Whitelist file does not exist")
            open(WHITELISTFILE, "a").close()
            
    
    def commands(self):
        mbgenres_cmd = ui.Subcommand("mbgenres", help="fetch genres from MusicBrainz")
        mbgenres_cmd.parser.add_option(
             "-F",
             "--force",
             action="store_true",
             dest="force",
             help="force genre updates"
        )
        mbgenres_cmd.func = self.mbgenres
        return [mbgenres_cmd]
    
    def mbgenres(self, lib, opts, args):
        write = ui.should_write()
        for album in lib.albums(ui.decargs(args)):
            self.writeAlbumTags(album, opts.force, write)

    def mbgenresImport(self, session, task):
        self.writeAlbumTags(task.album, True, True)

    # Fetch genres from MusicBrainz and write to database   
    def writeAlbumTags(self, album, force, write):

        # Always runs if forced or genre does not exist, otherwise checks if genre has been updated in the last x days (specified by updatefrequency in config)
        if force or not hasattr(album, "genreupdated") or (datetime.datetime.strptime(album.genreupdated, "%d/%m/%Y") + datetime.timedelta(days=self.config["updatefrequency"].get())) < datetime.datetime.now():              
            genre = self.config["fallback"].get()
            
            # Get tags from release and release groups
            mbgenres = list(set(self.getGenres("release", album.mb_albumid) + self.getGenres("release-group", album.mb_releasegroupid)))
            
            # If no genres are retrieved for the release or release group, fallback to the artist's genres if enabled
            if len(mbgenres) == 0 and self.config["artistfallback"].get():
                mbgenres = self.getGenres("artist", album.mb_albumartistid)
            
            # If genres were found, processes them
            if len(mbgenres) != 0:                
                # Sort by tag name
                mbgenres.sort(key=itemgetter(0))
                # Remove any duplicate tags between the release and release group, keeping the highest vote count
                mbgenres = [max(items) for key, items in groupby(mbgenres, key = itemgetter(0))]
                # Sort by vote count descending
                mbgenres.sort(key=itemgetter(1), reverse=True)
                
                # If more tags than the genre limit, and overflow is enabled 
                if len(mbgenres) > self.config["genrecount"].get() and self.config["dynamiccount"].get():
                    
                    # Get the count of the first tag that would be excluded
                    lowestgenrecount = mbgenres[self.config["genrecount"].get() - 1][1]
                    # If the tag meets the overflow limit, then select all tags including any overflows
                    if lowestgenrecount >= self.config["dynamicdivider"].get():
                        mbgenres = list(filter(lambda x: x[1] >= lowestgenrecount, mbgenres))
                    elif mbgenres[0][1] != mbgenres[len(mbgenres) - 1][1]:
                        # Filter the list to all vote counts that are greater than the first count after the genre limit
                        mbgenres = list(filter(lambda x: x[1] > mbgenres[self.config["genrecount"].get()][1], mbgenres))
                
                # Remove any tags above the max limit if overflow is disabled
                if not self.config["dynamiccount"].get():
                    mbgenres = mbgenres[slice(self.config["genrecount"].get())]

                # If genre replacement is configured to false, then combine the old genre list with the new genre list
                finalgenres = [x[0] for x in mbgenres]
                if not self.config["replace"].get() and album.genre:
                    oldgenres = album.genre.lower().split(self.config["separator"].get())
                    finalgenres = list(set(oldgenres + finalgenres))

                # Convert list into a separated genre string
                finalgenres.sort()
                genre = self.config["separator"].get().join(finalgenres);
            
            # If no genres were found on MusicBrainz and genre replacement is false, return
            elif not self.config["replace"].get() or self.config["fallback"].get() is None:
                return
            
            # Store results and set date updated field
            if genre:
                if self.config["titlecase"].get() and album.genre != genre.title():
                    album.genre = genre.title()
                elif album.genre != genre.lower():
                    album.genre = genre.lower()
                else:
                    return
            else:
                album.genre = genre

            self._log.info(u'Added genre(s) [{0}] to "{1}" by "{2}"',  album.genre, album.album, album.albumartist)
            album.genreupdated = datetime.datetime.now().strftime("%d/%m/%Y")
            album.store()
            for item in album.items():
                if write:
                        item.try_write()         
    
    def getGenres(self, type, mbid):
        headers = {"User-Agent": "BeetsPlugin-MBGenres/" + VERSION + " (mistwyrmdev@gmail.com)"}
        MAXTRIES = 5

        for attempt in range(0, MAXTRIES):
            try:
                # Wait one second between requests per MusicBrainz API documentation
                time.sleep(1)
                response = requests.get("https://musicbrainz.org/ws/2/" + type + "/" + mbid + "?inc=genres tags&fmt=json", headers)
                data = response.json()

                # Read genres
                genre_data = [
                    (genre["name"], int(genre["count"]))
                    for genre in data["genres"]
                    if int(genre["count"]) >= self.config["minvotes"].get()
                ]
                
                # Read whitelisted tags
                tag_data = [
                    (tag["name"], int(tag["count"]))
                    for tag in data["tags"]
                    if int(tag["count"]) >= self.config["minvotes"].get() and tag["name"].lower() in (item.lower() for item in self.whitelist)
                ]

                return list(set(genre_data + tag_data));
            except:
                self._log.debug(u"Attempt {0}: Problem contacting MusicBrainz, unable to fetch genres for {1} {2}", str(attempt+1), type, mbid)
                ++attempt
        self._log.debug(u"Unable to fetch genre for {0} {1} after {2} tries, skipping", type, mbid, MAXTRIES)
        return
        