"""
This File is part of bLUe software.

Copyright (C) 2017  Bernard Virot <bernard.virot@libertysurf.fr>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Lesser General Public License as
published by the Free Software Foundation, version 3.

This program is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
Lesser General Lesser Public License for more details.

You should have received a copy of the GNU Lesser General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.
"""
import sys

import numpy as np
from PIL import Image
from PIL.ImageCms import getOpenProfile, getProfileInfo, \
    buildTransformFromOpenProfiles, applyTransform, INTENT_PERCEPTUAL, ImageCmsProfile, PyCMSError, core
from PySide2.QtGui import QImage

from bLUeGui.bLUeImage import QImageBuffer
from bLUeGui.dialog import dlgWarn

from bLUeTop.settings import COLOR_MANAGE_OPT, SRGB_PROFILE_PATH, ADOBE_RGB_PROFILE_PATH, DEFAULT_MONITOR_PROFILE_PATH

if COLOR_MANAGE_OPT:
    if sys.platform == 'win32':
        import win32gui
    else:
        # python-gi flag
        HAS_GI = False
        try:
            from gi.repository import GLib, Gio, Colord
            HAS_GI = True
        except ImportError:
            pass
        if not HAS_GI:
            dlgWarn("Automatic detection of monitor profile needs gi installed.\n trying to use %s instead" % DEFAULT_MONITOR_PROFILE_PATH)
            try:
                getOpenProfile(DEFAULT_MONITOR_PROFILE_PATH)
            except PyCMSError:
                dlgWarn("Invalid profile %s" % DEFAULT_MONITOR_PROFILE_PATH, info="Color management is disabled")


def get_default_working_profile():
    """
    try to find a default image profile
    @return: profile
    @rtype: ImageCmsProfile
    """
    try:
        profile = getOpenProfile(SRGB_PROFILE_PATH)
    except PyCMSError:
        dlgWarn('No valid sRGB color profile found.\nSet SYSTEM_PROFILE_DIR and SRGB_PROFILE_NAME in your config.json',
                info='Invalid profile %s' % SRGB_PROFILE_PATH)
        sys.exit()
    return profile


def get_default_monitor_profile():
    """
    try to find a default image profile
    @return: profile or None
    @rtype: ImageCmsProfile or None
    """
    profile = None
    try:
        profile = getOpenProfile(DEFAULT_MONITOR_PROFILE_PATH)
    except PyCMSError:
        pass
    return profile


class icc:
    """
    Container for color management related options and methods.
    Should never be instantiated.
    """
    HAS_COLOR_MANAGE = False  # menu action "color manage" will be disabled
    COLOR_MANAGE = False  # no color management

    monitorProfile, workingProfile, workToMonTransform = (None,)*3
    workingProfileInfo, monitorProfileInfo = '', ''

    @staticmethod
    def B_get_display_profile(handle=None, device_id=None):
        """
        bLUe version of ImageCms get_display_profile.
        @param handle: screen handle (Windows)
        @type handle: int
        @param device_id: name of display
        @type device_id: str
        @return: monitor profile or None
        @rtype: ImageCmsProfile or None
        """
        profile_path = DEFAULT_MONITOR_PROFILE_PATH
        if sys.platform == "win32":
            profile_path = core.get_display_profile_win32(handle, 1)
        elif HAS_GI:
            try:
                from PySide2.QtWidgets import QApplication, QMainWindow
                GIO_CANCELLABLE = Gio.Cancellable.new()
                client = Colord.Client.new()
                client.connect_sync(GIO_CANCELLABLE)
                device = client.find_device_sync('xrandr-' + device_id, GIO_CANCELLABLE)
                device.connect_sync(GIO_CANCELLABLE)
                default_profile = device.get_default_profile()
                default_profile.connect_sync(GIO_CANCELLABLE)
                profile_path = default_profile.get_filename()
            except (NameError, ImportError, GLib.GError) as e:
                from bLUeTop.QtGui1 import window
                dlgWarn('Cannot detect monitor profile', info=str(e), parent=window)
        try:
            Cms_profile = getOpenProfile(profile_path)
        except PyCMSError:
            Cms_profile = get_default_monitor_profile()
        return Cms_profile

    @classmethod
    def getMonitorProfile(cls, qscreen=None):
        """
        Try to retrieve the default color profile
        associated to the monitor specified by QScreen
        (the system main display if qscreen is None).
        The method returns None if no profile can be found.
        @param qscreen: QScreen instance
        @type qscreen: QScreen
        @return: monitor profile or None
        @rtype: CmsProfile or None
        """
        monitorProfile = None
        # detecting profile
        if qscreen is not None:
            try:
                if sys.platform == 'win32':
                    dc = win32gui.CreateDC(str(qscreen.name()), None, None)
                    monitorProfile = cls.B_get_display_profile(handle=dc)
                else:
                    monitorProfile = cls.B_get_display_profile(device_id=qscreen.name())
            except (RuntimeError, OSError, TypeError):
                pass
        return monitorProfile

    @classmethod
    def configure(cls, qscreen=None, colorSpace=-1, workingProfile=None):
        """
        Try to configure color management for the monitor
        specified by QScreen, and build an image transformation
        from the working profile (default sRGB) to the monitor profile.
        This transformation is convenient to map image colors to screen colors.
        @param qscreen: QScreen instance
        @type qscreen: QScreep
        @param colorSpace:
        @type colorSpace
        @param workingProfile:
        @type workingProfile:
        """
        cls.HAS_COLOR_MANAGE = False
        # a (default) working image profile is always needed, at least for RGB<-->XYZ conversions
        cls.defaultWorkingProfile = get_default_working_profile()
        cls.workingProfile = cls.defaultWorkingProfile
        cls.workingProfileInfo = getProfileInfo(cls.workingProfile)
        if not COLOR_MANAGE_OPT:
            return
        # looking for valid profiles
        try:
            # get monitor profile as CmsProfile object.
            if qscreen is not None:
                cls.monitorProfile = cls.getMonitorProfile(qscreen=qscreen)
                if cls.monitorProfile is None:  # not handled by PIL
                    raise ValueError
                # get profile info, a PyCmsError exception is raised if monitorProfile is invalid
                cls.monitorProfileInfo = getProfileInfo(cls.monitorProfile)
            # get working profile as CmsProfile object
            if colorSpace == 1:
                cls.workingProfile = cls.defaultWorkingProfile  # getOpenProfile(SRGB_PROFILE_PATH)
            elif colorSpace == 2:
                cls.workingProfile = getOpenProfile(ADOBE_RGB_PROFILE_PATH)
            elif type(workingProfile) is ImageCmsProfile:
                cls.workingProfile = workingProfile
            else:
                cls.workingProfile = getOpenProfile(SRGB_PROFILE_PATH)  # default

            cls.workingProfileInfo = getProfileInfo(cls.workingProfile)
            # init CmsTransform object : working profile ---> monitor profile
            cls.workToMonTransform = buildTransformFromOpenProfiles(cls.workingProfile, cls.monitorProfile,
                                                                     "RGB", "RGB", renderingIntent=INTENT_PERCEPTUAL)
            """
                                    INTENT_PERCEPTUAL            = 0 (DEFAULT) (ImageCms.INTENT_PERCEPTUAL)
                                    INTENT_RELATIVE_COLORIMETRIC = 1 (ImageCms.INTENT_RELATIVE_COLORIMETRIC)
                                    INTENT_SATURATION            = 2 (ImageCms.INTENT_SATURATION)
                                    INTENT_ABSOLUTE_COLORIMETRIC = 3 (ImageCms.INTENT_ABSOLUTE_COLORIMETRIC)
            """
            cls.HAS_COLOR_MANAGE = (cls.monitorProfile is not None) and \
                                   (cls.workingProfile is not None) and (cls.workToMonTransform is not None)
            cls.COLOR_MANAGE = cls.HAS_COLOR_MANAGE and cls.COLOR_MANAGE
        except (OSError, IOError) as e:
            print("I/O error({0}): {1}".format(e.errno, e.strerror))
        except (ValueError, TypeError, PyCMSError):
            pass
        except:
            print("Unexpected error:", sys.exc_info()[0])
            raise

def cmsConvertQImage(image, cmsTransformation=None):
    """
    Apply a Cms transformation to a copy of a QImage and
    return the transformed image.
    If cmsTransformation is None, the input image is returned (no copy).
    @param image: image to transform
    @type image: QImage
    @param cmsTransformation : Cms transformation
    @type cmsTransformation: ImageCmsTransform
    @return: The converted QImage
    @rtype: QImage
    """
    if cmsTransformation is None:
        return image
    image = image.copy()
    buf = QImageBuffer(image)[:, :, :3][:, :, ::-1]
    # convert to the PIL context and apply cmsTransformation
    bufC = np.ascontiguousarray(buf)
    PIL_img = Image.frombuffer('RGB', (image.width(), image.height()), bufC, 'raw',
                               'RGB', 0, 1)  # these 3 weird parameters are recommended by a runtime warning !!!
    applyTransform(PIL_img, cmsTransformation, 1)  # 1=in place
    # back to the image buffer
    buf[...] = np.frombuffer(PIL_img.tobytes(), dtype=np.uint8).reshape(buf.shape)
    return image
