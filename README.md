This File is part of bLUe software.

Copyright (C) 2017-2018 Bernard Virot <bernard.virot@libertysurf.fr>

## DESCRIPTION

 bLUe is a complete GUI environment for photo edition, featuring a large set of adjustment layers and
 3D LUT management.

A 3D LUT is represented by a 3D cube of color nodes. Image pixels are associated
to nodes, based on their color and modifications are applied to each node individually.
bLUe proposes interactive 3D LUTs which can be edited by grouping and moving nodes over
a (hue, saturation) color wheel, making bLUe a powerful 3D LUT editor.

 bLUe is aware of multi-screen environments and color profiles : it uses image and
monitor profiles in conjunction to display accurate colors.

The program is fully modular : functionalities are implemented as independent
adjustment layers using a common GUI. Any imaging library exposing Python
bindings can take advantage of the GUI.

bLUe is written in Python.

## Functionalities

* Edition of files in formats jpg, png, tif, nef, cr2, dng.
* Adjustment layers : exposure, brightness, saturation, contrast, color temperature, inversion, filters, noise reduction, cloning,
segmentation, geometric transformations, curves (1D LUTs) and 3D LUTs.
* Automatic contrast enhancement
* Import and export of 3D LUTs in .cube format
* mask edition
* Editable (contrast) tone curve for raw files (cr2, nef, dng).

## REQUIREMENTS

* OpenCV-Python
* NumPy
* PySide2
* PIL
* RawPy
* PyWavelets

ExifTool must be installed.

Under Windows,  pywin32 is needed for multi-screen management.

Binary packages containing all dependencies are available for Windows.
Make sure to download the latest release.

## LICENSE

 This project is licensed under the LGPL V 3.

