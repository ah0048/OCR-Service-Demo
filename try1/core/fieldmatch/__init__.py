"""Image-similarity matching for the fixed-choice fields on the ID back.

Public surface:

``options``
    The closed vocabularies and Arabic variant folding.
``features``
    Crop -> scale-invariant descriptor, and descriptor comparison.
``bank``
    Build, harvest and cache the reference template bank.
``matcher``
    Match a crop to one option, enforce gender agreement, and write results.
``cluster``
    Group real harvested crops into clusters so a human can label them in bulk.
"""
