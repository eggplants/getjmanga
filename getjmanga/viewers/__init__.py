"""The viewers several sites share: what is the reader's, not the site's.

One module per viewer platform (or per JavaScript library a viewer runs),
holding the pure functions -- a key, a cipher, a tile permutation -- and,
where the viewer talks to a server the same way everywhere, the requests
too, as functions taking the extractor whose session to use. Nothing in
here is an `Extractor`; a site's module imports from here, never from
another site's.
"""
