#  This file is part of Lazylibrarian.
#  Lazylibrarian is free software':'you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#  Lazylibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#  You should have received a copy of the GNU General Public License
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>.

import threading
import traceback
from operator import itemgetter

import lazylibrarian
from lazylibrarian import logger, database
from lazylibrarian.images import getAuthorImage
from lazylibrarian.cache import cache_img
from lazylibrarian.formatter import today, unaccented, formatAuthorName, makeUnicode
from lazylibrarian.grsync import grfollow
from lazylibrarian.gb import GoogleBooks
from lazylibrarian.gr import GoodReads
from lib.fuzzywuzzy import fuzz
# noinspection PyUnresolvedReferences
from lib.six.moves import queue


def addAuthorNameToDB(author=None, refresh=False, addbooks=True):
    # get authors name in a consistent format, look them up in the database
    # if not in database, try to import them.
    # return authorname,authorid,new where new=False if author already in db, new=True if added
    # authorname returned is our preferred name, or empty string if not found or unable to add

    new = False
    if not author or len(author) < 2:
        logger.debug('Invalid Author Name [%s]' % author)
        return "", "", False

    author = formatAuthorName(author)
    myDB = database.DBConnection()

    # Check if the author exists, and import the author if not,
    check_exist_author = myDB.match('SELECT AuthorID FROM authors where AuthorName=?', (author.replace('"', '""'),))

    # If no exact match, look for a close fuzzy match to handle misspellings, accents
    if not check_exist_author:
        match_name = author.replace('"', '""').lower()
        res = myDB.action('select AuthorID,AuthorName from authors')
        for item in res:
            aname = item['AuthorName']
            if aname:
                match_fuzz = fuzz.ratio(aname.lower(), match_name)
                if match_fuzz >= 95:
                    logger.debug("Fuzzy match [%s] %s%% for [%s]" % (item['AuthorName'], match_fuzz, author))
                    check_exist_author = item
                    author = item['AuthorName']
                    break

    if not check_exist_author and lazylibrarian.CONFIG['ADD_AUTHOR']:
        logger.debug('Author %s not found in database, trying to add' % author)
        # no match for supplied author, but we're allowed to add new ones
        GR = GoodReads(author)
        try:
            author_gr = GR.find_author_id()
        except Exception as e:
            logger.warn("%s finding author id for [%s] %s" % (type(e).__name__, author, str(e)))
            return "", "", False

        # only try to add if GR data matches found author data
        if author_gr:
            authorname = author_gr['authorname']
            # authorid = author_gr['authorid']
            # "J.R.R. Tolkien" is the same person as "J. R. R. Tolkien" and "J R R Tolkien"
            match_auth = author.replace('.', ' ')
            match_auth = ' '.join(match_auth.split())

            match_name = authorname.replace('.', ' ')
            match_name = ' '.join(match_name.split())

            match_name = unaccented(match_name)
            match_auth = unaccented(match_auth)

            # allow a degree of fuzziness to cater for different accented character handling.
            # some author names have accents,
            # filename may have the accented or un-accented version of the character
            # The currently non-configurable value of fuzziness might need to go in config
            # We stored GoodReads unmodified author name in
            # author_gr, so store in LL db under that
            # fuzz.ratio doesn't lowercase for us
            match_fuzz = fuzz.ratio(match_auth.lower(), match_name.lower())
            if match_fuzz < 90:
                logger.debug("Failed to match author [%s] to authorname [%s] fuzz [%d]" %
                             (author, match_name, match_fuzz))

            # To save loading hundreds of books by unknown authors at GR or GB, ignore unknown
            if (author != "Unknown") and (match_fuzz >= 90):
                # use "intact" name for author that we stored in
                # GR author_dict, not one of the various mangled versions
                # otherwise the books appear to be by a different author!
                author = author_gr['authorname']
                authorid = author_gr['authorid']
                # this new authorname may already be in the
                # database, so check again
                check_exist_author = myDB.match('SELECT AuthorID FROM authors where AuthorID=?', (authorid,))
                if check_exist_author:
                    logger.debug('Found goodreads authorname %s in database' % author)
                else:
                    logger.info("Adding new author [%s]" % author)
                    try:
                        addAuthorToDB(authorname=author, refresh=refresh, authorid=authorid, addbooks=addbooks)
                        check_exist_author = myDB.match('SELECT AuthorID FROM authors where AuthorID=?', (authorid,))
                        if check_exist_author:
                            new = True
                    except Exception as e:
                        logger.error('Failed to add author [%s] to db: %s %s' % (author, type(e).__name__, str(e)))
    # check author exists in db, either newly loaded or already there
    if not check_exist_author:
        logger.debug("Failed to match author [%s] in database" % author)
        return "", "", False
    author = makeUnicode(author)
    return author, check_exist_author['AuthorID'], new


def addAuthorToDB(authorname=None, refresh=False, authorid=None, addbooks=True):
    """
    Add an author to the database by name or id, and optionally get a list of all their books
    If author already exists in database, refresh their details and optionally booklist
    """
    threadname = threading.currentThread().name
    if "Thread-" in threadname:
        threading.currentThread().name = "AddAuthorToDB"
    # noinspection PyBroadException
    try:
        myDB = database.DBConnection()
        match = False
        authorimg = ''
        new_author = not refresh
        entry_status = ''

        if authorid:
            dbauthor = myDB.match("SELECT * from authors WHERE AuthorID=?", (authorid,))
            if not dbauthor:
                authorname = 'unknown author'
                logger.debug("Adding new author id %s to database" % authorid)
                new_author = True
            else:
                entry_status = dbauthor['Status']
                authorname = dbauthor['authorname']
                logger.debug("Updating author %s " % authorname)
                new_author = False

            controlValueDict = {"AuthorID": authorid}
            newValueDict = {"Status": "Loading"}
            if new_author:
                newValueDict["AuthorName"] = "Loading"
                newValueDict["AuthorImg"] = "images/nophoto.png"
            myDB.upsert("authors", newValueDict, controlValueDict)

            GR = GoodReads(authorid)
            author = GR.get_author_info(authorid=authorid)
            if author:
                authorname = author['authorname']
                authorimg = author['authorimg']
                controlValueDict = {"AuthorID": authorid}
                newValueDict = {
                    "AuthorLink": author['authorlink'],
                    "DateAdded": today()
                }
                if not dbauthor or (dbauthor and not dbauthor['manual']):
                    newValueDict["AuthorImg"] = author['authorimg']
                    newValueDict["AuthorBorn"] = author['authorborn']
                    newValueDict["AuthorDeath"] = author['authordeath']
                    if not dbauthor:
                        newValueDict["AuthorName"] = author['authorname']
                    elif dbauthor['authorname'] != author['authorname']:
                        authorname = dbauthor['authorname']
                        logger.warn("Authorname mismatch for %s [%s][%s]" %
                                    (authorid, dbauthor['authorname'], author['authorname']))
                myDB.upsert("authors", newValueDict, controlValueDict)
                match = True
            else:
                logger.warn("Nothing found for %s" % authorid)
                if not dbauthor:
                    myDB.action('DELETE from authors WHERE AuthorID=?', (authorid,))

        if authorname and not match:
            authorname = ' '.join(authorname.split())  # ensure no extra whitespace
            GR = GoodReads(authorname)
            author = GR.find_author_id(refresh=refresh)

            query = "SELECT * from authors WHERE AuthorName=?"
            dbauthor = myDB.match(query, (authorname.replace("'", "''"),))
            if author and not dbauthor:  # may have different name for same authorid (spelling?)
                query = "SELECT * from authors WHERE AuthorID=?"
                dbauthor = myDB.match(query, (author['authorid'],))
                authorname = dbauthor['AuthorName']

            controlValueDict = {"AuthorName": authorname}

            if not dbauthor:
                newValueDict = {
                    "AuthorID": "0: %s" % authorname,
                    "Status": "Loading"
                }
                logger.debug("Now adding new author: %s to database" % authorname)
                entry_status = lazylibrarian.CONFIG['NEWAUTHOR_STATUS']
                new_author = True
            else:
                newValueDict = {"Status": "Loading"}
                logger.debug("Now updating author: %s" % authorname)
                entry_status = dbauthor['Status']
                new_author = False
            myDB.upsert("authors", newValueDict, controlValueDict)

            if author:
                authorid = author['authorid']
                authorimg = author['authorimg']
                controlValueDict = {"AuthorName": authorname}
                newValueDict = {
                    "AuthorID": author['authorid'],
                    "AuthorLink": author['authorlink'],
                    "DateAdded": today(),
                    "Status": "Loading"
                }
                if dbauthor:
                    if authorname != dbauthor['authorname']:
                        # name change might be users preference
                        logger.warn("Conflicting authorname for %s [%s][%s] Ignoring change" %
                                    (author['authorid'], authorname, dbauthor['authorname']))
                        authorname = dbauthor['authorname']
                        # cmd = 'UPDATE authors SET AuthorName=? WHERE AuthorName=?'
                        # myDB.action(cmd, (author['authorname'], dbauthor['authorname']))
                    if author['authorid'] != dbauthor['authorid']:
                        # GoodReads may have altered authorid?
                        logger.warn("Conflicting authorid for %s (%s:%s) Moving to new authorid" %
                                    (authorname, author['authorid'], dbauthor['authorid']))
                        cmd = 'UPDATE books SET AuthorID=? WHERE AuthorID=?'
                        myDB.action(cmd, (author['authorid'], dbauthor['authorid']))
                        myDB.action('DELETE from authors WHERE AuthorID=?', (dbauthor['authorid'],))
                        dbauthor = None

                if not dbauthor or (dbauthor and not dbauthor['manual']):
                    newValueDict["AuthorImg"] = author['authorimg']
                    newValueDict["AuthorBorn"] = author['authorborn']
                    newValueDict["AuthorDeath"] = author['authordeath']

                myDB.upsert("authors", newValueDict, controlValueDict)
                match = True
            else:
                logger.warn("Nothing found for %s" % authorname)
                if not dbauthor:
                    myDB.action('DELETE from authors WHERE AuthorName=?', (authorname,))
                return
        if not match:
            logger.error("No matching result for authorname or authorid")
            return

        # if author is set to manual, should we allow replacing 'nophoto' ?
        new_img = False
        match = myDB.match("SELECT Manual from authors WHERE AuthorID=?", (authorid,))
        if not match or not match['Manual']:
            if authorimg and 'nophoto' in authorimg:
                newimg = getAuthorImage(authorid)
                if newimg:
                    authorimg = newimg
                    new_img = True

        # allow caching
        if authorimg and authorimg.startswith('http'):
            newimg, success, _ = cache_img("author", authorid, authorimg, refresh=refresh)
            if success:
                authorimg = newimg
                new_img = True
            else:
                logger.debug('Failed to cache image for %s' % authorimg)

        if new_img:
            controlValueDict = {"AuthorID": authorid}
            newValueDict = {"AuthorImg": authorimg}
            myDB.upsert("authors", newValueDict, controlValueDict)

        if addbooks:
            if new_author:
                bookstatus = lazylibrarian.CONFIG['NEWAUTHOR_STATUS']
                audiostatus = lazylibrarian.CONFIG['NEWAUTHOR_AUDIO']
            else:
                bookstatus = lazylibrarian.CONFIG['NEWBOOK_STATUS']
                audiostatus = lazylibrarian.CONFIG['NEWAUDIO_STATUS']

            if entry_status not in ['Active', 'Wanted', 'Ignored', 'Paused']:
                entry_status = 'Active'  # default for invalid/unknown or "loading"
            # process books
            if lazylibrarian.CONFIG['BOOK_API'] == "GoogleBooks":
                if lazylibrarian.CONFIG['GB_API']:
                    book_api = GoogleBooks()
                    book_api.get_author_books(authorid, authorname, bookstatus=bookstatus,
                                              audiostatus=audiostatus, entrystatus=entry_status,
                                              refresh=refresh)
                # if lazylibrarian.CONFIG['GR_API']:
                #     book_api = GoodReads(authorname)
                #     book_api.get_author_books(authorid, authorname, bookstatus=bookstatus,
                #                               ausiostatus=audiostatus, entrystatus=entry_status,
                #                               refresh=refresh)
            elif lazylibrarian.CONFIG['BOOK_API'] == "GoodReads":
                if lazylibrarian.CONFIG['GR_API']:
                    book_api = GoodReads(authorname)
                    book_api.get_author_books(authorid, authorname, bookstatus=bookstatus,
                                              audiostatus=audiostatus, entrystatus=entry_status,
                                              refresh=refresh)
                # if lazylibrarian.CONFIG['GB_API']:
                #     book_api = GoogleBooks()
                #     book_api.get_author_books(authorid, authorname, bookstatus=bookstatus,
                #                               audiostatus=audiostatus, entrystatus=entry_status,
                #                               refresh=refresh)

            update_totals(authorid)

            if new_author and lazylibrarian.CONFIG['GR_FOLLOWNEW']:
                res = grfollow(authorid, True)
                if res.startswith('Unable'):
                    logger.warn(res)
                try:
                    followid = res.split("followid=")[1]
                    logger.debug('%s marked followed' % authorname)
                except IndexError:
                    followid = ''
                myDB.action('UPDATE authors SET GRfollow=? WHERE AuthorID=?', (followid, authorid))
        else:
            # if we're not loading any books, mark author as ignored
            entry_status = 'Ignored'

        controlValueDict = {"AuthorID": authorid}
        newValueDict = {"Status": entry_status}
        myDB.upsert("authors", newValueDict, controlValueDict)

        msg = "[%s] Author update complete, status %s" % (authorname, entry_status)
        logger.info(msg)
        return msg
    except Exception:
        msg = 'Unhandled exception in addAuthorToDB: %s' % traceback.format_exc()
        logger.error(msg)
        return msg


def update_totals(AuthorID):
    myDB = database.DBConnection()
    # author totals needs to be updated every time a book is marked differently
    match = myDB.select('SELECT AuthorID from authors WHERE AuthorID=?', (AuthorID,))
    if not match:
        logger.debug('Update_totals - authorid [%s] not found' % AuthorID)
        return

    cmd = 'SELECT BookName, BookLink, BookDate, BookID from books WHERE AuthorID=?'
    cmd += ' AND Status != "Ignored" order by BookDate DESC'
    lastbook = myDB.match(cmd, (AuthorID,))

    cmd = "select sum(case status when 'Ignored' then 0 else 1 end) as unignored,"
    cmd += "sum(case when status == 'Have' then 1 when status == 'Open' then 1 "
    cmd += "when audiostatus == 'Have' then 1 when audiostatus == 'Open' then 1 "
    cmd += "else 0 end) as have, count(*) as total from books where authorid=?"
    totals = myDB.match(cmd, (AuthorID,))

    controlValueDict = {"AuthorID": AuthorID}
    newValueDict = {
        "TotalBooks": totals['total'],
        "UnignoredBooks": totals['unignored'],
        "HaveBooks": totals['have'],
        "LastBook": lastbook['BookName'] if lastbook else None,
        "LastLink": lastbook['BookLink'] if lastbook else None,
        "LastBookID": lastbook['BookID'] if lastbook else None,
        "LastDate": lastbook['BookDate'] if lastbook else None
    }
    myDB.upsert("authors", newValueDict, controlValueDict)

    cmd = "select series.seriesid as Series,sum(case books.status when 'Ignored' then 0 else 1 end) as Total,"
    cmd += "sum(case when books.status == 'Have' then 1 when books.status == 'Open' then 1"
    cmd += " when books.audiostatus == 'Have' then 1 when books.audiostatus == 'Open' then 1"
    cmd += " else 0 end) as Have from books,member,series,seriesauthors where member.bookid=books.bookid"
    cmd += " and member.seriesid = series.seriesid and seriesauthors.seriesid = series.seriesid"
    cmd += " and seriesauthors.authorid=? group by series.seriesid"
    res = myDB.select(cmd, (AuthorID,))
    if len(res):
        for series in res:
            myDB.action('UPDATE series SET Have=?, Total=? WHERE SeriesID=?',
                        (series['Have'], series['Total'], series['Series']))

    res = myDB.match('SELECT AuthorName from authors WHERE AuthorID=?', (AuthorID,))
    logger.debug('Updated totals for [%s]' % res['AuthorName'])


def import_book(bookid, wait=False):
    """ search goodreads or googlebooks for a bookid and import the book """
    if lazylibrarian.CONFIG['BOOK_API'] == "GoogleBooks":
        GB = GoogleBooks(bookid)
        if not wait:
            _ = threading.Thread(target=GB.find_book, name='GB-IMPORT', args=[bookid, "Wanted"]).start()
        else:
            GB.find_book(bookid, "Wanted")
    else:  # lazylibrarian.CONFIG['BOOK_API'] == "GoodReads":
        GR = GoodReads(bookid)
        if not wait:
            _ = threading.Thread(target=GR.find_book, name='GR-IMPORT', args=[bookid, "Wanted"]).start()
        else:
            GR.find_book(bookid, "Wanted")


def search_for(searchterm):
    """ search goodreads or googlebooks for a searchterm, return a list of results
    """
    if lazylibrarian.CONFIG['BOOK_API'] == "GoogleBooks":
        GB = GoogleBooks(searchterm)
        myqueue = queue.Queue()
        search_api = threading.Thread(target=GB.find_results, name='GB-RESULTS', args=[searchterm, myqueue])
        search_api.start()
    else:  # lazylibrarian.CONFIG['BOOK_API'] == "GoodReads":
        myqueue = queue.Queue()
        GR = GoodReads(searchterm)
        search_api = threading.Thread(target=GR.find_results, name='GR-RESULTS', args=[searchterm, myqueue])
        search_api.start()

    search_api.join()
    searchresults = myqueue.get()
    sortedlist = sorted(searchresults, key=itemgetter('highest_fuzz', 'num_reviews'), reverse=True)
    return sortedlist
