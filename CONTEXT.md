# book-search

Finds ebooks and audiobooks, brings them into the shared Calibre library, and
sends ebooks on to a Kindle. It also watches Anca's Goodreads wishlist and
fetches what she adds without anyone in the loop.

## Language

### Goodreads pipeline

**Wishlist**:
Anca's Goodreads `to-read` shelf, the only shelf whose additions are fetched.
_Avoid_: lists, reading list, Goodreads lists

**Addition**:
A book that appears on the wishlist after the pipeline started watching it. The
books already there when it started are seeded and never fetched.
_Avoid_: new book, backlog

**Candidate**:
One downloadable file a source offers for an addition, described by its library record.
_Avoid_: result, hit, row

**Confident match**:
A candidate the matcher accepts on its own: the ISBN agrees, or the title and the
author's surname both agree after normalizing.
_Avoid_: match, fuzzy match

**Claude check**:
Claude's yes/no judgement that a confident match is the book she asked for,
made on the library record before download and on the file's own contents after.
_Avoid_: verification, review, approval

**Miss**:
An addition that ends without a book: not found, no confident match, no English
edition, or every candidate refused by the Claude check.
_Avoid_: failure, error

**Error**:
An addition given up on after repeated outages (a source, the Claude check, or the
import), as distinct from a miss, where the sources answered.
_Avoid_: miss

### Delivery

**Kindle send**:
Emailing a book from Calibre to a Kindle address. The Goodreads pipeline does it
for every book it fetches, apart from the deliberate skips.
_Avoid_: forward, push, sync

**Skip**:
An addition deliberately not sent to her Kindle although Calibre holds it: it was
there before she added it, it exists only as a PDF, or it is too large for the
mail relay.
_Avoid_: failed send

**Share**:
A book sent to book-search by hand from a phone, naming its own recipient.
_Avoid_: shortcut download, manual ingest
