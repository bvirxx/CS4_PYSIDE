"""
Copyright (C) 2017  Bernard Virot

PeLUT - Photo editing software using adjustment layers with 1D and 3D Look Up Tables.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>
"""

import subprocess
import os
import json
from PyQt4.QtGui import QTransform, QMessageBox

####################################################
# exiftool path
EXIFTOOL_PATH = "H:\standalone\exiftool\exiftool(-k)"
# EXIFTOOL_PATH = "C:\standalone\exiftool(-k)"
#####################################################

class ExifTool(object):
    """
    exiftool wrapper
    """
    sentinel = "{ready}"
    # exiftool flags
    # -n : print numerical values
    # -j : json output
    # -a : extract duplicate tags
    # -S : very short output format
    # -G0 : print group name for each tag
    flags = ["-j", "-a", "-n", "-S", "-G0", "-Orientation", "-ProfileDescription", "-colorSpace", "-InteropIndex", "-WhitePoint", "-PrimaryChromaticities", "-Gamma"]#, "-ICC_Profile:all"]
    extract_profile_flags = ["-icc_profile", "-b"]

    def __init__(self, executable = EXIFTOOL_PATH):
        self.executable = executable

    # enter/exit "with" block
    def __enter__(self):
        try:
            self.process = subprocess.Popen(
                                        [self.executable, "-stay_open", "True",  "-@", "-"],
                                        stdin =subprocess.PIPE, stdout =subprocess.PIPE, stderr =subprocess.STDOUT
                                       )
        except OSError:
            msg = QMessageBox()
            msg.setText("cannot execute exiftool :\nset EXIFTOOL_PATH in file exiftool.py")
            msg.exec_()
            exit()
        return self

    def  __exit__(self, exc_type, exc_value, traceback):
        self.process.stdin.write("-stay_open\nFalse\n")
        self.process.stdin.flush()

    def execute(self, *args):
        args = args + ("-execute\n",)
        self.process.stdin.write(str.join("\n", args))
        self.process.stdin.flush()
        output = ""
        fd = self.process.stdout.fileno()
        while not output[:-2].endswith(self.sentinel):
            output += os.read(fd, 4096)
        return output[:-len(self.sentinel)-2]

    def get_metadata(self, *filenames):
        #return json.loads(self.execute("-G", "-j", "-n", *filenames))
        profile=self.execute(*(self.extract_profile_flags + list(filenames)))
        return profile, json.loads(self.execute(*(self.flags+list(filenames))))


def decodeExifOrientation(value):
    """
    Returns a QTransform object representing the
    image transformation corresponding to the orientation tag value
    :param value: orientation tag
    :return: Qtransform object
    """
    # identity transformation
    tr = QTransform()
    if value == 0:
        pass
    elif value == 1 :
        pass
    elif value == 6:   # TODO complete
        tr.rotate(90)
    else :
        raise ValueError("decodeExifOrientation : unhandled orientation tag: %d" % value)
    return tr

"""
case `jpegexiforient -n "$i"` in
 1) transform="";;
 2) transform="-flip horizontal";;
 3) transform="-rotate 180";;
 4) transform="-flip vertical";;
 5) transform="-transpose";;
 6) transform="-rotate 90";;
 7) transform="-transverse";;
 8) transform="-rotate 270";;
 *) transform="";;
 esac
"""