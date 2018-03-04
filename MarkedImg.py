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
import multiprocessing
import types
import weakref
from PySide2 import QtCore
from functools import partial

import gc

import rawpy
from PySide2.QtCore import Qt, QBuffer, QDataStream, QFile, QIODevice, QSize, QPointF, QPoint, QRectF, QByteArray

import cv2
from copy import copy

from PySide2.QtGui import QImageWriter, QImageReader, QTransform, QBrush
from PySide2.QtWidgets import QApplication, QMessageBox, QSplitter
from PySide2.QtGui import QPixmap, QImage, QColor, QPainter
from PySide2.QtCore import QRect

from colorConv import sRGB2LabVec, sRGBWP, Lab2sRGBVec, sRGB_lin2XYZ, sRGB_lin2XYZInverse, rgb2rgbLinearVec, rgbLinear2rgb, \
    rgbLinear2rgbVec
from denoising import denoise
from grabcut import segmentForm
from graphicsFilter import filterIndex
from icc import convertQImage
import icc
from imgconvert import *
from colorCube import interpVec, rgb2hspVec, hsp2rgbVec, LUT3DIdentity, LUT3D, interpVec_
from time import time
from utils import savitzky_golay, channelValues, checkeredImage, boundingRect
import graphicsHist
# pool is created in QLayer.applyToStack()
MULTIPROC_POOLSIZE = 4

class ColorSpace:
    notSpecified = -1; sRGB = 1

class metadata:
    """
    Container for vImage meta data
    """
    def __init__(self, name=''):
        self.name, self.colorSpace, self.rawMetadata, self.profile, self.orientation, self.rating = name, ColorSpace.notSpecified, [], '', None, 5

class vImage(QImage):
    """
    Versatile image class.
    This is the base class for all multi-layered and interactive image classes.
    It gathers all image information, including meta-data.
    A vImage holds three images: full (self), thumbnail (self.thumb) and
    hald (self.hald) for LUT3D conversion. Note that self.thumb and self.hald are not
    cache buffers : self, self.thumb and self.hald are initialized
    and handled identically.
    Each image owns a mask (disabled by default).
    """
    # max thumbSize
    thumbSize = 1000
    # default colors
    defaultBgColor = QColor(191, 191, 191)
    # To be able to display masks as color masks, we use the red channel to code
    # mask opacity, instead of alpha channel.
    # When modifying these colors, it is mandatory to
    # modify the methods invertMask and color2opacityMask accordingly.
    defaultColor_UnMasked = QColor(128, 0, 0, 255)
    defaultColor_Masked = QColor(0, 0, 0, 255)
    defaultColor_Invalid = QColor(0, 99, 0, 255)
    @classmethod
    def color2OpacityMask(cls, mask):
        mask = mask.copy()
        buf = QImageBuffer(mask)
        buf[:, :, 3] = np.where(buf[:, :, 2] == 0, 0, 255)
        return mask
    @classmethod
    def visualizeMask(cls, img, mask, color=True, clipping=False):
        # copy image
        img = QImage(img)
        qp = QPainter(img)
        qp.drawImage(QRect(0, 0, img.width(), img.height()), img)
        if color:
            # draw mask as color mask with opacity 0.5
            qp.setCompositionMode(QPainter.CompositionMode_SourceOver)
            # tmp = tmp.copy()
            tmp = mask.copy()
            tmpBuf = QImageBuffer(tmp)
            tmpBuf[:, :, 3] = 128
            """
            if self.isClipping:
                tmpBuf[:, :, 3] = 128 #255 - tmpBuf[:, :, 3]  # TODO modified 6/11/17 for clipping background
                #tmpBuf[:, :, :3] = 64                         # TODO modified 6/11/17 for clipping background
            else:
                tmpBuf[:, :, 3] = 128  # 255 - tmpBuf[:,:,3]
            """
            qp.drawImage(QRect(0, 0, img.width(), img.height()), tmp)
        else:
            # draw mask as opacity mask : mode DestinationIn sets image opacity to mask opacity
            qp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
            qp.drawImage(QRect(0, 0, img.width(), img.height()), vImage.color2OpacityMask(mask))
            if clipping:  # TODO 6/11/17 may be we should draw checker for both selected and unselected mask
                qp.setCompositionMode(QPainter.CompositionMode_DestinationOver)
                qp.drawImage(QRect(0, 0, img.width(), img.height()), checkeredImage(img.width(), img.height()))
        qp.end()
        return img
    @ classmethod
    def seamlessMerge(cls, dest, source, mask, cloningMethod):
        """
        Seamless merging of source into dest.
        source must provide a mask defining
        the region to be copied. The same mask is used to define
        the target region in dest.
        The dest image is modified.
        @param dest:
        @type dest: vImage
        @param source:
        @type source: vImage
        @param cloningMethod:
        @type cloningMethod:
        """
        # init white mask from scaled mask
        src_mask = vImage.color2OpacityMask(mask).scaled(source.size())
        buf = QImageBuffer(src_mask)
        tmp = np.where(buf[:, :, 3] == 0, 0, 255)
        # get scaled bounding rect
        bRect = boundingRect(tmp, 255)
        if bRect is None:
            # no white pixels
            return
        src_maskBuf = np.dstack((tmp, tmp, tmp)).astype(np.uint8)
        # cloning center is the center of bRect
        center = (bRect.left() + bRect.width() // 2, bRect.top() + bRect.height() // 2)
        sourceBuf = QImageBuffer(source)
        destBuf = QImageBuffer(dest)
        output = cv2.seamlessClone(sourceBuf[:, :, :3][:, :, ::-1],  # source
                                   destBuf[:, :, :3][:, :, ::-1],  # dest
                                   src_maskBuf,
                                   center, cloningMethod
                                   )
        destBuf[:, :, :3][:, :, ::-1] = output  # assign src_ maskBuf for testing


    def __init__(self, filename=None, cv2Img=None, QImg=None, mask=None, format=QImage.Format_ARGB32,
                                            name='', colorSpace=-1, orientation=None, rating=5, meta=None, rawMetadata=[], profile=''):
        """
        With no parameter, builds a null image.
        Mask is disabled by default.
        image is assumed to be in the color space sRGB : colorSpace value is used only as meta data.
        @param filename: path to file
        @type filename: str
        @param cv2Img: data buffer
        @type cv2Img: ndarray
        @param QImg: image
        @type QImg: QImage
        @param mask: Image mask. Should have format and dims identical to those of image
        @type mask: QImage
        @param format: QImage format (default QImage.Format_ARGB32)
        @type format: QImage.Format
        @param name: image name
        @type name: str
        @param colorSpace: color space (default : notSpecified)
        @type colorSpace: MarkedImg.colorSpace
        @param orientation: Qtransform object (default None)
        @type orientation: Qtransform 
        @param meta: meta data (default None)
        @type meta: MarkedImg.metadata
        @param rawMetadata: list of dictionaries (default [])
        @type rawMetadata: list of dictionaries
        @param profile: embedded profile (default '')
        @type profile: str
        """
        # color management : working profile is assumed for image
        self.colorTransformation = icc.workToMonTransform
        # current color managed image
        self.cmImage = None
        self.isModified = False
        self.rect, self.mask, = None, mask
        self.filename = filename if filename is not None else ''
        self.isCropped = False
        self.isRuled = False

        # mode flags
        self.useHald = False
        self.hald = None
        self.isHald = False
        self.useThumb = False

        # Cache buffers
        self.cachesEnabled = True
        self.qPixmap = None
        self.rPixmap = None
        self.hspbBuffer = None
        self.LabBuffer = None

        # preview image.
        # Conceptually, the layer stack can be seen as
        # the juxtaposition of two stacks:
        #  - a stack of full sized images
        #  - a stack of thumbnails
        # For the sake of performance, the two stacks are
        # NOT synchronized : they are updated independently.
        # Thus, after initialization, the thumbnail should NOT be computed from
        # the full size image.
        self.thumb = None
        self.onImageChanged = lambda: 0
        if meta is None:
            # init container
            self.meta = metadata()
            self.meta.name, self.meta.colorSpace, self.meta.rawMetadata, self.meta.profile, self.meta.orientation, self.meta.rating = name, colorSpace, rawMetadata, profile, orientation, rating
        else:
            self.meta = meta
        if (filename is None and cv2Img is None and QImg is None):
            # create a null image
            super(vImage, self).__init__()
        if filename is not None:
            # load image from file (should be a 8 bits/channel color image)
            if self.meta.orientation is not None:
                tmp = QImage(filename, format=format).transformed(self.meta.orientation)
            else:
                tmp = QImage(filename, format=format)
            # ensure format is format !!
            tmp = tmp.convertToFormat(format)
            if tmp.isNull():
                raise ValueError('Cannot load %s\nSupported image formats\n%s' % (filename, QImageReader.supportedImageFormats()))
            # call to super is mandatory. Shallow copy : no harm !
            super(vImage, self).__init__(tmp)
        elif QImg is not None:
            # build image from QImage, shallow copy
            super(vImage, self).__init__(QImg)
            if hasattr(QImg, "meta"):
                self.meta = copy(QImg.meta)
        elif cv2Img is not None:
            # build image from buffer
            super(vImage, self).__init__(ndarrayToQImage(cv2Img, format=format))
        # check format
        if self.depth() != 32:
            raise ValueError('vImage : should be a 8 bits/channel color image')
        # init mask
        self.maskIsEnabled = False
        self.maskIsSelected = False
        if self.mask is None:
            self.mask = QImage(self.width(), self.height(), format)
            # default : unmask all
            self.mask.fill(self.defaultColor_UnMasked)
        #self.updatePixmap()
        if type(self) in [QLayer]:
            vImage.updatePixmap(self)

    def setImage(self, qimg):
        """
        copy qimg to image. Does not update metadata.
        image and qimg must have identical dimensions and type.
        @param qimg: QImage object
        @type qimg: QImage
        @param update:
        @type update: boolean
        """
        buf1, buf2 = QImageBuffer(self), QImageBuffer(qimg)
        if buf1.shape != buf2.shape:
            raise ValueError("QLayer.setImage : new image and layer must have identical shapes")
        buf1[...] = buf2
        self.thumb = None
        self.cacheInvalidate()
        self.updatePixmap()

    def initThumb(self):
        """
        Init the image thumbnail as (scaled) QImage. In contrast to
        maskedThumbContainer, thumb is never used as an input image, thus
        there is no need for a type yielding color space buffers.
        However, note that, for convenience, layer thumbs own an attribute
        parentImage set by the overridden method QLayer.initThumb.
        For non adjustment layers, the thumbnail will never be updated. So, we
        perform a high quality scaling.
        """
        scImg = self.scaled(self.thumbSize, self.thumbSize, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        # With the Qt.SmoothTransformation flag, the scaled image format is premultiplied
        self.thumb = scImg.convertToFormat(QImage.Format_ARGB32, Qt.DiffuseDither | Qt.DiffuseAlphaDither)

    def getThumb(self):
        """
        inits image thumbnail if needed and returns it.
        @return: thumbnail
        @rtype: QImage
        """
        if self.thumb is None:
            self.initThumb()
        return self.thumb

    def initHald(self):
        """
        Builds a hald image (as a QImage) from identity 3D LUT.
        """
        if not self.cachesEnabled:
            return
        s = int((LUT3DIdentity.size )**(3.0/2.0)) + 1
        buf0 = LUT3DIdentity.getHaldImage(s, s)
        self.Hald = QImage(QSize(s,s), QImage.Format_ARGB32)
        buf1 = QImageBuffer(self.Hald)
        buf1[:,:,:]=buf0
        buf1[:, :, 3] = 255  # TODO added 15/11/17 for coherence with the overriding function QLayer.initHald()

    def resize_coeff(self, widget):
        """
        Normalization of self.Zoom_coeff.
        Return the current resizing coefficient, used by
        the paint event handler to display the image.
        Note 1. This coefficient is chosen to initially (i.e. when self.Zoom_coeff = 1)
        fill the widget.
        Note 2. To correctly manage splitted views, we use the size of the QSplitter parent
        container instead of the size of the widget.
        @param widget: Qwidget object
        @return: the (multiplicative) resizing coefficient
        """
        if type(widget.parent()) == QSplitter:
            widget = widget.parent()
        r_w, r_h = float(widget.width()) / self.width(), float(widget.height()) / self.height()
        r = max(r_w, r_h)
        return r * self.Zoom_coeff

    def getCurrentImage(self):
        """
        Returns current (full, preview or hald) image,
        according to the values of the flag useThumb and useHald.
        The thumbnail and hald are computed if they are not initialized.
        Otherwise, they are not updated, unless self.thumb is None
        or purgeThumb is True.
        The method is overridden in QLayer
        @return: image
        @rtype: QImage
        """
        if self.useHald:
            return self.getHald()
        if self.useThumb:
            return self.getThumb()
        else:
            return self

    def full2CurrentXY(self, x, y):
        """
        Maps x,y coordinates of pixel in the full image to
        coordinates in current image.
        @param x:
        @type x: int or float
        @param y:
        @type y: int or float
        @return:
        @rtype: 2uple of int
        """
        if self.useThumb:
            currentImg = self.getThumb()
            x = (x * currentImg.width()) / self.width()
            y = (y * currentImg.height()) / self.height()
        return int(x), int(y)

    def getHspbBuffer(self):
        """
        returns the image buffer in color mode HSpB.
        The buffer is recalculated if needed and cached.
        The method is overridden in QLayer
        @return: HSPB buffer 
        @rtype: ndarray
        """
        #inputImage = self.inputImgFull().getCurrentImage()
        if self.hspbBuffer is None:
            currentImage = self.getCurrentImage()
            self.hspbBuffer = rgb2hspVec(QImageBuffer(currentImage)[:,:,:3][:,:,::-1])
        return self.hspbBuffer

    def getLabBuffer(self):
        """
        returns the image buffer in color mode Lab.
        The buffer is calculated if needed and cached.
        The method is overridden in QLayer
        @return: Lab buffer 
        @rtype: numpy ndarray, dtype numpy.float64
        """
        if self.LabBuffer is None:
            currentImage = self.getCurrentImage()
            self.LabBuffer = sRGB2LabVec(QImageBuffer(currentImage)[:, :, :3][:, :, ::-1])
        return self.LabBuffer

    def setModified(self, b):
        """
        Sets the flag isModified and calls handler.
        @param b: flag
        @type b: boolean
        """
        self.isModified = b

    def cacheInvalidate(self):
        """
        Invalidates buffers.
        """
        self.hspbBuffer = None
        self.LabBuffer = None
        #self.thumb = None #TODO thumb is not a cache 12/10/17
        if hasattr(self, 'maskedImageContainer'):
            if self.maskedImageContainer is not None:
                self.maskedImageContainer.cacheInvalidate()
        if hasattr(self, 'maskedThumbContainer'):
            if self.maskedThumbContainer is not None:
                self.maskedThumbContainer.cacheInvalidate()

    def updatePixmap(self, maskOnly=False):
        """
        Updates the caches qPixmap, thumb and cmImage.
        The image is that returned by getCurrentImage(), thus
        the caches are synchronized using the current image
        mode (full or preview).

        If maskOnly is True, cmImage is not updated.
        if maskIsEnabled is False, the mask is not shown.
        If maskIsEnabled is True, then
            - if maskIsSelected is True, the mask is drawn over
              the layer as a color and opacity mask, with its own
              pixel color and inverse opacity.
            - if maskIsSelected is False, the mask is drawn as an
              opacity mask, setting image opacity to that of mask
              (mode DestinationIn). Color mask is no used.
        NOTE : the fully masked part of the image corresponds to
        mask opacity = 0.
        @param maskOnly: default False
        @type maskOnly: boolean
        """
        currentImage = self.getCurrentImage() #vImage.getCurrentImage(self)   modified 03/10/17
        if not maskOnly:
            # invalidate color managed cache
            self.cmImage = None
        # get (eventually) up to date  color managed image
        if icc.COLOR_MANAGE:
            if self.cmImage is None:
                # CAUTION : reset alpha channel
                img = convertQImage(currentImage, transformation=self.colorTransformation)
                # restore alpha
                buf0 = QImageBuffer(img)
                buf1 = QImageBuffer(currentImage)
                buf0[:, :, 3] = buf1[:, :, 3]
            else:
                #img = QImage(self.cmImage)
                img = self.cmImage
        else:
            #img = QImage(currentImage)
            img = currentImage
        # refresh cache
        """
        if maskOnly:
            #self.cmImage = QImage(img)
            self.cmImage = img
        """
        self.cmImage = img
        qImg = img
        rImg = currentImage
        if self.maskIsEnabled:
            qImg = vImage.visualizeMask(qImg, self.mask, color=self.maskIsSelected, clipping=self.isClipping)
            rImg = vImage.visualizeMask(rImg, self.mask, color=self.maskIsSelected, clipping=self.isClipping)
        self.qPixmap = QPixmap.fromImage(qImg)
        self.rPixmap = QPixmap.fromImage(rImg)

    def resetMask(self, maskAll=False):
        # default : nothing is masked
        self.mask.fill(vImage.defaultColor_Masked if maskAll else vImage.defaultColor_UnMasked)
        self.updatePixmap(maskOnly=True)

    def invertMask(self):
        """
        Inverts mask: masked/unmasked pixels
        are coded by red = 0/128
        """
        buf = QImageBuffer(self.mask)
        buf[:, :,2] = 128 - buf[:,:,2]  #np.where(buf[:,:,2]==128, 0, 128)

    def resize(self, pixels, interpolation=cv2.INTER_CUBIC):
        """
        Resizes an image while keeping its aspect ratio. We use
        the opencv function cv2.resize() to perform the resizing operation, so we
        can choose the resizing method (default cv2.INTER_CUBIC)
        The original image is not modified.
        @param pixels: pixel count for the resized image
        @type pixels: int
        @param interpolation: interpolation method (default cv2.INTER_CUBIC)
        @type interpolation:
        @return : the resized vImage
        @rtype : vImage
        """
        ratio = self.width() / float(self.height())
        w, h = int(np.sqrt(pixels * ratio)), int(np.sqrt(pixels / ratio))
        hom = w / float(self.width())
        # resizing
        Buf = QImageBuffer(self)
        cv2Img = cv2.resize(Buf, (w, h), interpolation=interpolation)
        rszd = vImage(cv2Img=cv2Img, meta=copy(self.meta), format=self.format())
        # prevent buffer from garbage collector
        rszd.dummy = cv2Img
        #resize rect and mask
        if self.rect is not None:
            rszd.rect = QRect(self.rect.left() * hom, self.rect.top() * hom, self.rect.width() * hom, self.rect.height() * hom)
        if self.mask is not None:
            # tmp.mask=cv2.resize(self.mask, (w,h), interpolation=cv2.INTER_NEAREST )
            rszd.mask = self.mask.scaled(w, h)
        self.setModified(True)
        return rszd

    def bTransformed(self, transformation):
        img = vImage(QImg=self.transformed(transformation))
        img.meta = self.meta
        img.onImageChanged = self.onImageChanged
        img.useThumb = self.useThumb
        img.useHald = self.useHald
        return img

    def bResized(self, w, h):
        transform = QTransform()
        transform = transform.scale(w/self.width(), h/self.height())
        return self.bTransformed(transform)

    def applyCloning(self, seamless=True):
        """
        The function draws the translated and zoomed
        input image on the output image.
        if seamless and self.cloned are both False,
        no seamless cloning is done.
        Otherwise, a seamless cloning to the non masked region
        of the output image is done.
        @param seamless:
        @type seamless: boolean
        """
        imgIn = self.inputImg()
        imgOut = self.getCurrentImage()
        # draw the translated and zoomed input image on the output image
        if not self.cloned :
            # erase previous transformed image : reset imgOut to ImgIn
            qp = QPainter(imgOut)
            qp.setCompositionMode(QPainter.CompositionMode_Source)
            qp.drawImage(QRect(0, 0, imgOut.width(), imgOut.height()), imgIn)
            # get translation relative to current Image
            currentAltX, currentAltY = self.full2CurrentXY(self.xAltOffset, self.yAltOffset)
            # draw translated and zoomed input image (nothing is drawn outside of dest. image)
            qp.setCompositionMode(QPainter.CompositionMode_SourceOver)
            rect = QRectF(currentAltX, currentAltY, imgOut.width()*self.AltZoom_coeff, imgOut.height()*self.AltZoom_coeff)
            qp.drawImage(rect, imgIn)
            qp.drawRect(rect)
            qp.end()
        # do seamless cloning
        if seamless or self.cloned:
            imgInc = imgIn.copy()
            src = imgOut
            vImage.seamlessMerge(imgInc, src, self.mask, self.cloningMethod)
            bufOut = QImageBuffer(imgOut)
            bufOut[:, :, :3] = QImageBuffer(imgInc)[:,:,:3]
        self.updatePixmap()

    def applyKnitting(self):
        """


        """
        imgIn = self.inputImg()
        imgOut = self.getCurrentImage()
        imgInc = imgIn.copy()
        src = self.parentImage.layersStack[self.sourceIndex].getCurrentImage()
        vImage.seamlessMerge(imgInc, src, self.mask, self.cloningMethod)
        bufOut = QImageBuffer(imgOut)
        bufOut[:, :, :3] = QImageBuffer(imgInc)[:,:,:3]
        self.updatePixmap()

    def applyGrabcut(self, nbIter=2, mode=cv2.GC_INIT_WITH_MASK):
        """
        Segmentation
        @param nbIter:
        @type nbIter: int
        @param mode:
        """
        inputImg = self.inputImg()
        rect = self.rect
        # resizing coeff fitting selection rectangle with current image
        r = inputImg.width() / self.width()

        finalMask = np.zeros((inputImg.height(), inputImg.width()), dtype=np.uint8) + cv2.GC_BGD
        # set pixels inside selection rectangle to PR_FGD
        if rect is not None:
            finalMask[int(rect.top() * r):int(rect.bottom() * r), int(rect.left() * r):int(rect.right() * r)] = cv2.GC_PR_FGD
        # add info from self.mask
        if inputImg.size() != self.size():
            scaledMask = self.mask.scaled(inputImg.width(), inputImg.height())
        else:
            scaledMask = self.mask
        scaledMaskBuf = QImageBuffer(scaledMask)
        finalMask[(scaledMaskBuf[:, :, 2] > 100 )* (scaledMaskBuf[:,:,1]!=99)] = cv2.GC_FGD
        finalMask[(scaledMaskBuf[:,:, 2]==0) *(scaledMaskBuf[:,:,1]!=99)] = cv2.GC_BGD
        finalMask_test = finalMask.copy()
        finalMask_save = finalMask.copy()
        # at least one FGD pixel and one BGD pixel
        if not ((np.any(finalMask == cv2.GC_FGD) or np.any(finalMask == cv2.GC_PR_FGD)) and (
            np.any(finalMask == cv2.GC_BGD) or np.any(finalMask == cv2.GC_PR_BGD))):
            reply = QMessageBox()
            reply.setText('You must select some background or foreground pixels')
            reply.setInformativeText('Use selection rectangle or draw mask')
            reply.setStandardButtons(QMessageBox.Ok)
            reply.exec_()
            return
        bgdmodel = np.zeros((1, 13 * 5), np.float64)  # Temporary array for the background model
        fgdmodel = np.zeros((1, 13 * 5), np.float64)  # Temporary array for the foreground model
        t0 = time()
        inputBuf = QImageBuffer(inputImg)
        bgdmodel_test = np.zeros((1, 13 * 5), np.float64)  # Temporary array for the background model
        fgdmodel_test = np.zeros((1, 13 * 5), np.float64)  # Temporary array for the foreground model

        cv2.grabCut_mt(inputBuf[:, :, :3], finalMask, None,  # QRect2tuple(img0_r.rect),
                        bgdmodel, fgdmodel, nbIter, mode)
        print ('grabcut_mtd time : %.2f' % (time()-t0))
        """
        t1 = time()
        cv2.grabCut(inputBuf[:, :, :3], finalMask_test, None,  # QRect2tuple(img0_r.rect),
                        bgdmodel_test, fgdmodel_test, nbIter, mode)
        print('grabcut time : %.2f' % (time() - t1))
        """
        # keep initial FGD and BGD pixels
        finalMask = np.where((finalMask_save==cv2.GC_BGD) + (finalMask_save == cv2.GC_FGD), finalMask_save, finalMask)

        # set opacity (255=background, 0=foreground)
        # We want to keep the colors of mask pixels. Unfortunately,
        # while drawing or scaling, Qt replaces the colors of transparent pixels by 0.
        # So, we can't set now mask alpha channel.
        finalOpacity = np.where((finalMask == cv2.GC_FGD) + (finalMask == cv2.GC_PR_FGD), 255, 0)
        buf = QImageBuffer(scaledMask)
        buf[:,:,2] = np.where(finalOpacity==255, 128, 0)
        # validate mask
        buf[:,:,1] = 0
        if self.size() != scaledMask.size():
            self.mask = scaledMask.scaled(self.size())
        else:
            self.mask = scaledMask

        self.updatePixmap()

    def applyExposure(self, exposureCorrection, options):
        """
        Apply exposure correction 2**exposureCorrection
        to linearized RGB channels.

        @param exposureCorrection:
        @type exposureCorrection: float
        @param options:
        @type options:
        @return:
        @rtype:
        """
        # neutral point
        if abs(exposureCorrection) < 0.05:
            buf0 = QImageBuffer(self.getCurrentImage())
            buf1 = QImageBuffer(self.inputImg())
            buf0[:, :, :] = buf1
            self.updatePixmap()
            return
        bufIn = QImageBuffer(self.inputImg())
        buf = bufIn[:,:,:3][:,:,::-1]
        buf = rgb2rgbLinearVec(buf)

        buf[:,:,:] = buf * (2 ** exposureCorrection)

        buf = rgbLinear2rgbVec(buf)

        buf = np.clip(buf, 0.0, 255.0)
        currentImage = self.getCurrentImage()
        ndImg1a = QImageBuffer(currentImage)
        ndImg1a[:, :, :3][:, :, ::-1] = buf
        # forward opacity
        ndImg1a[:, :, 3] = bufIn[:,:,3] # TODO 23/10/17 fix
        self.updatePixmap()

    def applyTransForm(self, transformation, options):
        if transformation.isIdentity():
            buf0 = QImageBuffer(self.getCurrentImage())
            buf1 = QImageBuffer(self.inputImg())
            buf0[:, :, :] = buf1
            self.updatePixmap()
            return
        inImg = self.inputImg()
        img = inImg.transformed(transformation).copy(QRect(-self.rectTrans.x(), -self.rectTrans.y(), inImg.width(), inImg.height()))
        outBuf = QImageBuffer(self.getCurrentImage())
        if img.isNull():
            print('null', img.rect())
            return
        outBuf[:,:,:] = QImageBuffer(img)
        self.updatePixmap()

    def applyRawPostProcessing(self):
        """
        Develop raw image: attribute
        self.parentImage.rawImage is mandatory
        """
        adjustForm = self.view.widget()
        options = adjustForm.options
        currentImage = self.getCurrentImage()
        bufOut = QImageBuffer(currentImage)
        """
        if self.postProcessCache is not None:
            if ((adjustForm.contCorrection == 0) and (self.postProcessCache.dtype==np.uint16)) or\
                                          ((adjustForm.contCorrection > 0) and (self.postProcessCache.dtype==np.uint8)):
                self.postProcessCache = None
        """

        # process raw image, 16 bits mode
        if self.postProcessCache is None:
            bufpost = self.parentImage.rawImage.postprocess(
                                        exp_shift=2.0**adjustForm.expCorrection,
                                        no_auto_bright= (not options['Auto Brightness']),
                                        use_auto_wb=options['Auto WB'],
                                        use_camera_wb=options['Camera WB'],
                                        user_wb = adjustForm.rawMultipliers,
                                        #gamma= (2.222, 4.5)  # default REC BT 709 exponent, slope
                                        gamma=(2.4, 12.92), # sRGB exponent, slope cf. https://en.wikipedia.org/wiki/SRGB#The_sRGB_transfer_function_("gamma")
                                        exp_preserve_highlights = 0.8 if options['Preserve Highlights'] else 0.0,
                                        output_bps=16
                                                            )
            self.postProcessCache = np.copy(bufpost)
        else:
            bufpost = self.postProcessCache

        needLab =  adjustForm.contCorrection > 0 or adjustForm.filterEnd > 4
        if needLab:
            # 16 bits image --> RGB float32 (range 0..1) --> Lab float32 L range 0..100 (opencv convention)
            buf32Lab = cv2.cvtColor(((bufpost.astype(np.float32))/65536).astype(np.float32), cv2.COLOR_RGB2Lab)
        """
        ####################
        # Gradual neutral density filter
        # We blend a neutral filter with density range 0.5*s...0.5 with the image b,
        # using the overlay blending mode : f(a,b) = 2*a*b if b < 0.5 else f(a,b) = 1 - 2*(1-a)(1-b)
        ####################
        h = bufpost.shape[0]
        rect = getattr(self, 'rect', None)
        if rect is not None:
            rect = rect.intersected(QRect(0,0, bufpost.shape[1], bufpost.shape[0]))
            adjustForm.filterStart = int((rect.top()/h)*100.0)
            adjustForm.filterEnd = int((rect.bottom()/h)*100.0)
            adjustForm.sliderFilterRange.setStart(adjustForm.filterStart)
            adjustForm.sliderFilterRange.setEnd(adjustForm.filterEnd)
        if adjustForm.filterEnd > 4:
            s = 0  #strongest 0
            opacity = 1 - s

            start = int(h * adjustForm.filterStart / 100.0)
            end = int(h * adjustForm.filterEnd / 100.0)
            test = np.array(range(end - start)) * opacity / (2.0 * max(end - start -1, 1)) + 0.5*s  # range 0.5*s...0.5
            test = np.concatenate((np.zeros(start) + 0.5*s, test, np.zeros(h - end) + 0.5))
            test1 = test[:, np.newaxis]
            Lchan = buf32Lab[:, :, 0]
            buf32Lab[:, :, 0] = np.where(Lchan < 50, Lchan * (test1 * 2.0), 100.0 - 2.0 * (1.0 - test1) * (100.0 - Lchan))
            #luminosity correction
            #buf32Lab[:,:,0] = buf32Lab[:,:,0]*(1.0+0.1)
        """
        ###########
        # contrast.
        # Clahe performs badly on small images :
        # we keep full resolution.
        ###########
        if adjustForm.contCorrection > 0:
            clahe = cv2.createCLAHE(clipLimit=adjustForm.contCorrection/2, tileGridSize=(8, 8))
            clahe.setClipLimit(adjustForm.contCorrection/2)
            # convert L channel to CV_16UC1 image and apply clahe
            res = clahe.apply(((buf32Lab[:, :, 0]*655.35).astype(np.uint16)))
            # convert back to float32
            res = ((res.astype(np.float32))/655.35).astype(np.float32)
            buf32Lab[:, :, 0] = res
        #############
        # convert to uint8
        #############
        if needLab:
            bufRGB32 = cv2.cvtColor(buf32Lab, cv2.COLOR_Lab2RGB)
            bufpost = (bufRGB32*255.0).astype(np.uint8)
        else:
            bufpost = (bufpost/256.0).astype(np.uint8)
        ###############
        # saturation
        ###############
        if adjustForm.satCorrection != 0:
            # convert to HSV
            bufHsB = cv2.cvtColor(bufpost, cv2.COLOR_RGB2HSV)
            alpha = (-adjustForm.satCorrection+50.0)/50
            start = time()
            # tabulate x**alpha
            LUT = (np.power(np.array(range(256))/255, alpha) * 255).astype(np.uint8)
            # convert saturation s to s**alpha
            bufHsB[:,:,1] = LUT[bufHsB[:,:,1]]
            print('sat time: %.5f' % (time() - start))
            # back to RGB
            bufpost = cv2.cvtColor(bufHsB, cv2.COLOR_HSV2RGB)

        if self.parentImage.useThumb:
            bufpost = cv2.resize(bufpost, (currentImage.width(), currentImage.height()))
        ################################################################################
        # denoising
        # opencv fastNlMeansDenoisingColored is very slow (>30s for a 15 Mpx image).
        # bilateral filtering is faster but not very accurate.
        ################################################################################
        """
        bufpost = cv2.bilateralFilter(bufpost,
                                      9 if self.parentImage.useThumb else 15,   # 21:5.5s, 15:3.5s, diameter of
                                                                                # (coordinate) pixel neighborhood,
                                                                                # 5 is the recommended value for fast processing
                                      10 * adjustForm.noiseCorrection,          # std deviation sigma
                                                                                # in color space,  100 middle value
                                      50 if self.parentImage.useThumb else 150, # std deviation sigma
                                                                                # in coordinate space,  100 middle value
                                      )
        """
        #bufpost = cv2.fastNlMeansDenoisingColored(bufpost, None, 3, 3, 7 ,21)  # hluminance, hcolor,  last params window sizes 7, 21 are recommended values
        noisecorr = adjustForm.noiseCorrection
        if noisecorr > 0:
            bufpost = cv2.fastNlMeansDenoisingColored(bufpost, None, 1+noisecorr, 1+noisecorr, 7, 21)
        bufOut[:, :, :3][:, :, ::-1] = bufpost
        self.updatePixmap()

    def applyCLAHE(self, clipLimit, options):
        #TODO define neutral point

        inputImage = self.inputImg()
        # get l channel

        LBuf = np.array(inputImage.getLabBuffer(), copy=True)
        # apply CLAHE to L channel
        clahe = cv2.createCLAHE(clipLimit=clipLimit, tileGridSize=(8, 8))
        clahe.setClipLimit(clipLimit)
        res = clahe.apply((LBuf[:,:,0] * 255.0).astype(np.uint8))
        LBuf[:,:,0] = res / 255
        sRGBBuf = Lab2sRGBVec(LBuf)
        # clipping is mandatory here : numpy bug ?
        #sRGBBuf = np.clip(sRGBBuf, 0, 255)
        currentImage = self.getCurrentImage()
        ndImg1a = QImageBuffer(currentImage)
        ndImg1a[:, :, :3][:,:,::-1] = sRGBBuf
        tmpBuf = QImageBuffer(inputImage)
        # forward opacity
        ndImg1a[:, :,3] = tmpBuf[:,:,3]
        self.updatePixmap()

    def apply1DLUT(self, stackedLUT, options={}):
        """
        Applies 1D LUTS (one for each channel)
        @param stackedLUT: array of color values (in range 0..255) : a line for each channel
        @type stackedLUT : ndarray, shape (3, 255), dtype int
        @param options: not used yet
        @type options : dictionary
        """
        #inputImage = self.inputImgFull().getCurrentImage()
        inputImage = self.inputImg()
        currentImage = self.getCurrentImage()
        #ndImg0 = QImageBuffer(inputImage)[:, :, :3]
        #ndImg1 = QImageBuffer(currentImage)[:, :, :3]
        # get image buffers (BGR order on intel arch.)
        ndImg0a = QImageBuffer(inputImage)
        ndImg1a = QImageBuffer(currentImage)
        ndImg0 = ndImg0a[:,:,:3]
        ndImg1 = ndImg1a[:, :, :3]
        # apply LUTS to channels
        rList = np.array([2,1,0]) #BGR
        ndImg1[:, :, :]= stackedLUT[rList[np.newaxis,:], ndImg0]
        # alpha propagation
        #ndImg1a[:,:,3] = ndImg0a[:,:,3]
        # update
        self.updatePixmap()

    def applyLab1DLUT(self, stackedLUT, options={}):
        """
        Applies 1D LUTS (one for each L,a,b channel)
        @param stackedLUT: array of color values (in range 0..255). Shape must be (3, 255) : a line for each channel
        @param options: not used yet
        """
        from colorConv import Lab2sRGBVec
        # Lab mode
        ndLabImg0 = self.inputImg().getLabBuffer()

        # apply LUTS to channels
        def scaleLabBuf(buf):
            buf = buf + [0.0, 128.0, 128.0]
            buf = buf * [255.0, 1.0, 1.0]# [255.0, 255.0/210.0, 255.0/210.0]
            return buf
        def scaleBackLabBuf(buf):
            #buf = np.dstack((buf[:, :, 0] / 255.0, buf[:, :, 1] * (210.0/255.0), buf[:, :, 2] * (210.0/ 255.0)))
            buf = np.dstack((buf[:, :, 0] / 255.0, buf[:, :, 1] , buf[:, :, 2] ))
            buf = buf - [0.0, 128.0, 128.0]
            return buf
        ndLImg0 = scaleLabBuf(ndLabImg0).astype(int)  #TODO problem here with astype(int) conversion
        #ndLImg0 = (ndLabImg0 * [1.0, 255.0, 255.0]).astype(int)
        rList = np.array([0, 1, 2])  # Lab
        ndLabImg1 = stackedLUT[rList[np.newaxis, :], ndLImg0]
        # LUT = stackedLUT[2,:]
        # ndLImg1 = stackedLUT[ndLImg0]
        ndLabImg1 = scaleBackLabBuf(ndLabImg1) #np.dstack((ndLabImg1[:, :, 0] / 255.0, ndLabImg1[:, :, 1] * (210.0/255.0), ndLabImg1[:, :, 2] * (210.0/ 255.0)))
        # back sRGB conversion
        ndsRGBImg1 = Lab2sRGBVec(ndLabImg1)
        # clipping is mandatory here : numpy bug ?
        ndsRGBImg1 = np.clip(ndsRGBImg1, 0, 255)
        currentImage = self.getCurrentImage()
        ndImg1a = QImageBuffer(currentImage)[:, :, :3]
        ndImg1 = ndImg1a[:,:,:3]
        ndImg1[:, :, ::-1] = ndsRGBImg1
        # alpha propagation
        #ndImg0 = QImageBuffer(self.InputImg())
        #ndImg1a[:, :, 3] = ndImg0[:, :, 3]
        # update
        self.updatePixmap()

    def applyHSPB1DLUT(self, stackedLUT, options={}, pool=None):
        """
        Applies 1D LUTS to hue, sat and brightness channels). A 1D LUT
        is an array of 255 color values (in range 0..255), which tabulates
        a color curve.
        @param stackedLUT: array of color values (in range 0..255), a line for each channel
        @type stackedLUT : ndarray, shape (3, 255), dtype int
        @param options: not used yet
        @type options : dictionary
        @param pool: multiprocessing pool : unused
        @type pool: muliprocessing.Pool
        """
        # neutral point
        if not np.any((stackedLUT - np.vstack((range(0,256), range(0,256), range(0,256))))) :
            buf1 = QImageBuffer(self.inputImg())
            buf2=QImageBuffer(self.getCurrentImage())
            buf2[:,:,:] = buf1
            self.updatePixmap()
            return
        # enter hald mode
        #self.parentImage.useHald = True

        # get updated HSpB buffer for inputImg
        #self.hspbBuffer = None
        ndHSPBImg0 = self.inputImg().getHspbBuffer()   # time 2s with cache disabled for 15 Mpx
        # apply LUTS to normalized channels (range 0..255)
        ndLImg0 = (ndHSPBImg0 * [255.0/360.0, 255.0, 255.0]).astype(int)
        rList = np.array([0,1,2]) # H,S,B
        ndLImg1 = stackedLUT[rList[np.newaxis,:], ndLImg0] * [360.0/255.0, 1/255.0, 1/255.0]
        ndHSBPImg1 = ndLImg1
        # back to sRGB
        ndRGBImg1 = hsp2rgbVec(ndHSBPImg1)  # time 4s for 15 Mpx
        # clipping is mandatory here : numpy bug ?
        ndRGBImg1 = np.clip(ndRGBImg1, 0, 255)
        # set current image to modified image
        currentImage = self.getCurrentImage()
        ndImg1a = QImageBuffer(currentImage)
        ndImg1 = ndImg1a[:,:,:3]
        ndImg1[:,:,::-1] = ndRGBImg1

        """
        Mode useHald is slower : Overhead 2s for 15Mpx
        # apply transformation in mode useHald
        outputHald = self.getCurrentImage()
        self.parentImage.useHald = False
        self.applyHald(outputHald, pool=pool)
        """
        # update
        self.updatePixmap()

    def apply3DLUT(self, LUT, options={}, pool=None):
        """
        Applies a 3D LUT to the current view of the image (self or self.thumb or self.hald).
        If pool is not None and the size of the current view is > 3000000, the computation is
        done in parallel on image slices.
        @param LUT: LUT3D array (see module colorCube.py)
        @type LUT: 3d ndarray, dtype = int
        @param options:
        @type options: dict of string:boolean pairs
        """
        # buffers of current image and current input image
        inputImage = self.inputImg()
        currentImage = self.getCurrentImage()


        # get selection
        w1, w2, h1, h2 = (0.0,) * 4
        if options.get('use selection', False):
            w, wF = self.getCurrentImage().width(), self.width()
            h, hF = self.getCurrentImage().height(), self.height()
            wRatio, hRatio = float(w) / wF, float(h) / hF
            if self.rect is not None:
                w1, w2, h1, h2 = int(self.rect.left() * wRatio), int(self.rect.right() * wRatio), int(self.rect.top() * hRatio), int(self.rect.bottom() * hRatio)
            w1 , h1 = max(w1,0), max(h1, 0)
            w2, h2 = min(w2, inputImage.width()), min(h2, inputImage.height())
            if w1>=w2 or h1>=h2:
                msg = QMessageBox()
                msg.setText("Empty selection\nSelect a region with the marquee tool")
                msg.exec_()
                return

        else:
            w1, w2, h1, h2 = 0, self.inputImg().width(), 0, self.inputImg().height()

        inputBuffer = QImageBuffer(inputImage)[h1:h2 + 1, w1:w2 + 1, :]
        imgBuffer = QImageBuffer(currentImage)[:, :, :]
        ndImg0 = inputBuffer[:, :, :3]
        ndImg1 = imgBuffer[:, :, :3]

        # choose right interpolation method
        if (pool is not None) and (inputImage.width() * inputImage.height() > 3000000):
            interp = interpVec
        else:
            interp = interpVec_
        # apply LUT
        start=time()
        ndImg1[h1:h2 + 1, w1:w2 + 1, :] = interp(LUT, ndImg0, pool=pool)
        end=time()
        #print 'Apply3DLUT time %.2f' % (end-start)
        # propagate mask ????
        #currentImage.mask = self.inputImgFull().getCurrentImage().mask
        #self.mask = self.inputImgFull().mask
        # alpha propagation
        #ndImg1a[:, :, 3] = ndImg0a[:, :, 3]
        self.updatePixmap()

    def applyHald(self, hald, pool=None):
        """
        Converts a hald image to a 3DLUT object and applies
        the 3D LUT to the current view, using a pool of parallel processes if
        pool is not None.
        @param hald: hald image
        @type hald: QImage
        @param pool: pool of parallel processes, default None
        @type pool: multiprocessing.pool
        """
        lut = LUT3D.HaldImage2LUT3D(hald)
        self.apply3DLUT(lut.LUT3DArray, options={'use selection' : False}, pool=pool)

    def histogram(self, size=QSize(200, 200), bgColor=Qt.white, range =(0,255), chans=channelValues.RGB, chanColors=Qt.gray, mode='RGB', addMode=''):
        """
        Plots the histogram of the image for the
        specified color mode and channels.
        @param size: size of the histogram plot
        @type size: int or QSize
        @param bgColor: background color
        @type bgColor: QColor
        @param range: plot data range
        @type range: 2-uple of int or float
        @param chans: channels to plot b=0, G=1, R=2
        @type chans: list of indices
        @param chanColors: color or 3-uple of colors
        @type chanColors: QColor or 3-uple of QColor 
        @param mode: color mode ((one of 'RGB', 'HSpB', 'Lab')
        @type mode: str
        @return: histogram plot
        @rtype: QImage
        """
        # type of size must be QSize
        if type(size) is int:
            size = QSize(size, size)
        # scaling factor for the bin edges
        spread = float(range[1] - range[0])
        scale = size.width() / spread
        # per channel histogram function
        def drawChannelHistogram(qp, channel, buf, color):
            """
            Computes and draws the (smoothed) histogram of the image for a single channel.
            @ param qp: QPainter
            @param channel: channel index (BGRA (intel) or ARGB )
            """
            buf0 = buf[:,:, channel]
            # bins='auto' sometimes causes huge number of bins ( >= 10**9) and memory error
            # even for small data size (<=250000), so we don't use it.
            # This is a numpy bug : in module function_base.py, to prevent memory error,
            # a reasonable upper bound for bins should be chosen.
            hist, bin_edges = np.histogram(buf0, bins=100, density=True)
            # smooth hist
            hist = savitzky_golay(hist, 11, 3)
            p = len(hist) - len(bin_edges)
            # P > 1 iff hist was padded by savitzky_golay filter
            # then, we pad bin_edges accordingly
            if p >1:
                p = p /2 +1
                bin_firstVals = (bin_edges[0] - np.arange(p))[::-1]
                bin_lastVals = bin_edges[-1] + np.arange(p-1)
                bin_edges = np.concatenate((bin_firstVals, bin_edges, bin_lastVals))
            # draw hist
            imgH = size.height()
            M = max(hist)
            lg = len(hist)
            for i, y in enumerate(hist):
                h = int(imgH * y / M)
                rect = QRect(int((bin_edges[i] - range[0]) * scale), max(img.height() - h, 0), int((bin_edges[i + 1] - bin_edges[i] + 1) * scale), h)
                # first and last bins are used to indicate possible clipping
                qp.fillRect(rect, color if (i > 0 and i < lg - 1) else Qt.cyan)
        bufL = cv2.cvtColor(QImageBuffer(self)[:, :, :3], cv2.COLOR_BGR2GRAY)[..., np.newaxis]  # returns Y (YCrCb) : Y = 0.299*R + 0.587*G+0.114*B
        if mode == 'RGB':
            buf = QImageBuffer(self)[:,:,:3][:,:,::-1]  #RGB
        elif mode == 'HSpB':
            buf = self.getHspbBuffer()
        elif mode == 'Lab':
            buf = self.getLabBuffer()
        elif mode =='Luminosity':
            chans = []
        img = QImage(size.width(), size.height(), QImage.Format_ARGB32)
        img.fill(bgColor)
        qp = QPainter(img)
        qp.setOpacity(0.75)
        if type(chanColors) is QColor or type(chanColors) is Qt.GlobalColor:
            chanColors = [chanColors]*3
        for ch in chans:
            drawChannelHistogram(qp, ch, buf, chanColors[ch])
        if mode=='Luminosity' or addMode=='Luminosity':
            drawChannelHistogram(qp, 0, bufL, Qt.darkGray)
        qp.end()
        buf = QImageBuffer(img)
        return img

    def applyFilter2D(self):
        """
        Applies 2D kernel. Available kernels are
        sharpen, unsharp, gaussian_blur
        @param radius: filter radius
        @type radius: float
        @param filter: filter type
        @type filter: filterIndex object (enum)
        @return: 
        """
        adjustForm = self.view.widget()
        inputImage = self.inputImg()
        currentImage = self.getCurrentImage()
        buf0 = QImageBuffer(inputImage)
        buf1 = QImageBuffer(currentImage)
        r = inputImage.width() / self.width()
        if self.rect is not None:
            rect = self.rect
            ROI0 = buf0[int(rect.top() * r): int(rect.bottom() * r), int(rect.left() * r): int(rect.right() * r),:3]
            # reset output image
            buf1[:,:,:] = buf0
            ROI1 = buf1[int(rect.top() * r): int(rect.bottom() * r), int(rect.left() * r): int(rect.right() * r),:3]
        else:
            ROI0 = buf0[:,:,:3]
            ROI1 = buf1[:,:,:3]
        if adjustForm.kernelCategory in [filterIndex.IDENTITY, filterIndex.UNSHARP, filterIndex.SHARPEN, filterIndex.BLUR1, filterIndex.BLUR2]:
            ROI1[:,:,:] = cv2.filter2D(ROI0, -1, adjustForm.kernel)
        else:
            radius = adjustForm.radius
            if self.parentImage.useThumb:
                radius = int(radius * r)
            ROI1[:,:,::-1] = cv2.bilateralFilter( ROI0[:,:,::-1], radius, 10*adjustForm.tone, 50 if self.parentImage.useThumb else 200)
        self.updatePixmap()

    def applyBlendFilter(self):
        adjustForm = self.view.widget()
        inputImage = self.inputImg()
        currentImage = self.getCurrentImage()
        buf0 = QImageBuffer(inputImage)
        buf1 = QImageBuffer(currentImage)
        r = inputImage.width() / self.width()

        ####################
        # Gradual neutral density filter
        # We blend a neutral filter with density range 0.5*s...0.5 with the image b,
        # using the overlay blending mode : f(a,b) = 2*a*b if b < 0.5 else f(a,b) = 1 - 2*(1-a)(1-b)
        ####################
        # image --> RGB float32 (range 0..1) --> Lab float32 L range 0..100 (opencv convention)
        buf32Lab = cv2.cvtColor(((buf0.astype(np.float32)) / 256).astype(np.float32), cv2.COLOR_BGR2Lab)
        h = buf0.shape[0]
        rect = getattr(self, 'rect', None)
        if rect is not None:
            rect = rect.intersected(QRect(0, 0, buf0.shape[1], buf0.shape[0]))
            adjustForm.filterStart = int((rect.top() / h) * 100.0)
            adjustForm.filterEnd = int((rect.bottom() / h) * 100.0)
            adjustForm.sliderFilterRange.setStart(adjustForm.filterStart)
            adjustForm.sliderFilterRange.setEnd(adjustForm.filterEnd)
        if adjustForm.filterEnd > 4:
            s = 0  # strongest 0
            opacity = 1 - s
            Lchan = buf32Lab[:, :, 0]
            start = int(h * adjustForm.filterStart / 100.0)
            end = int(h * adjustForm.filterEnd / 100.0)
            test = np.array(range(end - start)) * opacity / (
                        2.0 * max(end - start - 1, 1)) + 0.5 * s  # range 0.5*s...0.5
            test = np.concatenate((np.zeros(start) + 0.5 * s, test, np.zeros(h - end) + 0.5))
            test1 = test[:, np.newaxis] + np.zeros(Lchan.shape)

            test1 = cv2.rotate(test1, cv2.ROTATE_180)
            buf32Lab[:, :, 0] = np.where(Lchan < 50, Lchan * (test1 * 2.0),
                                         100.0 - 2.0 * (1.0 - test1) * (100.0 - Lchan))
            # luminosity correction
            # buf32Lab[:,:,0] = buf32Lab[:,:,0]*(1.0+0.1)
            bufRGB32 = cv2.cvtColor(buf32Lab, cv2.COLOR_Lab2RGB)
            buf1[:,:,:3][:,:,::-1] = (bufRGB32 * 255.0).astype(np.uint8)
        self.updatePixmap()

    def applyTemperature(self, temperature, options):
        """
        The method implements two algorithms for the correction of color temperature.
        1) Chromatic adaptation : linear transformation in the XYZ color space with Bradford
        cone response. cf. http://www.brucelindbloom.com/index.html?Eqn_ChromAdapt.html
        This boils down to use multipliers in the cone response domain.
        2) Photo filter : Blending using mode multiply, plus correction of luminosity
        @param temperature:
        @type temperature: float
        @param options :
        @type options : dictionary
        """
        inputImage = self.inputImg()
        currentImage = self.getCurrentImage()
        # neutral point : forward input image and return
        if abs(temperature -6500) < 200:
            buf0 = QImageBuffer(currentImage)
            buf1 = QImageBuffer(inputImage)
            buf0[:, :, :] = buf1
            self.updatePixmap()
            return
        from blend import blendLuminosity
        from colorConv import bbTemperature2RGB, conversionMatrix, rgb2rgbLinearVec, rgbLinear2rgbVec
        if options['Photo Filter']:
            # get black body color
            r, g, b = bbTemperature2RGB(temperature)
            filter = QImage(inputImage.size(), inputImage.format())
            filter.fill(QColor(r, g, b, 255))
            # draw image on filter using mode multiply
            qp = QPainter(filter)
            qp.setCompositionMode(QPainter.CompositionMode_Multiply)
            qp.drawImage(0, 0, inputImage)
            qp.end()
            # correct the luminosity of the resulting image,
            # by blending it with the inputImage, using mode luminosity.
            # We use a tuning coeff to control the amount of correction.
            # Note that using perceptual brightness gives better results, unfortunately slower
            resImg = blendLuminosity(filter, inputImage)
            bufOutRGB = QImageBuffer(resImg)[:,:,:3][:,:,::-1]
        elif options['Chromatic Adaptation']:
            # get conversion matrix in the XYZ color space
            M = conversionMatrix(temperature, sRGBWP)  # source is input image : sRGB, ref WP D65
            buf = QImageBuffer(inputImage)[:, :, :3]
            # opencv cvtColor does NOT perform gamma conversion
            # for RGB<-->XYZ cf. http://docs.opencv.org/trunk/de/d25/imgproc_color_conversions.html#color_convert_rgb_xyz.
            # Moreover, RGB-->XYZ and XYZ-->RGB matrices are not inverse transformations!
            # This yields incorrect results.
            #  As a workaround, we first convert to rgbLinear,
            # and use the sRGB2XYZ and sRGB2XYZInverse matrices from
            # http://www.brucelindbloom.com/index.html?Eqn_RGB_XYZ_Matrix.html
            # convert to RGB Linear
            bufLinear = (rgb2rgbLinearVec(buf[:,:,::-1])*255).astype(np.uint8)
            # convert to XYZ
            bufXYZ = np.tensordot(bufLinear, sRGB_lin2XYZ, axes=(-1, -1))
            # apply conversion matrix
            bufXYZ = np.tensordot(bufXYZ, M, axes=(-1, -1))
            # convert back to RGBLinear
            bufOutRGBLinear = np.tensordot(bufXYZ, sRGB_lin2XYZInverse, axes=(-1, -1))
            # convert back to RGB
            bufOutRGB = rgbLinear2rgbVec(bufOutRGBLinear.astype(np.float)/255.0)
            bufOutRGB = (bufOutRGB.clip(0, 255)).astype(np.uint8)
        # set output image
        bufOut = QImageBuffer(currentImage)[:,:,:3]
        bufOut[:, :, ::-1] = bufOutRGB
        self.updatePixmap()

class mImage(vImage):
    """
    Multi-layer image. A mImage object holds at least a background
    layer. All layers share the same metadata object. To correctly render a
    mImage, widgets must override their paint event handler.
    """

    def __init__(self, *args, **kwargs):
        # as updatePixmap uses layersStack, must be before super __init__
        #self._layers = {}
        self.layersStack = []
        # link back to QLayerView window
        self.layerView = None
        super(mImage, self).__init__(*args, **kwargs)
        # add background layer
        bgLayer = QLayer.fromImage(self, parentImage=self)
        bgLayer.isClipping = True
        self.setModified(False)
        self.activeLayerIndex = None
        self.addLayer(bgLayer, name='background')
        self.isModified = False
        # rawpy object
        self.rawImage = None

    def bTransformed(self, transformation):
        img = mImage(QImg=self.transformed(transformation))
        img.meta = self.meta
        img.onImageChanged = self.onImageChanged
        img.useThumb = self.useThumb
        img.useHald = self.useHald
        stack = []
        for layer in self.layersStack:
            tLayer = layer.bTransformed(transformation, img)
            stack.append(tLayer)
        img.layersStack = stack
        gc.collect()
        return img

    def getActiveLayer(self):
        """
        Returns the currently active layer.
        @return: The active layer
        @rtype: QLayer
        """
        if self.activeLayerIndex is not None:
            return self.layersStack[self.activeLayerIndex]
        else:
            return None

    def setActiveLayer(self, stackIndex):
        """
        Assigns stackIndex value to  activeLayerIndex and
        updates the layer view and tools
        @param stackIndex: index in stack for the layer to select
        @type stackIndex: int
        @return: active layer
        @rtype: QLayer
        """
        lgStack = len(self.layersStack)
        if stackIndex < 0 or stackIndex >= lgStack:
            return
        # clean old active layer
        active = self.getActiveLayer()
        if active is not None:
            if active.tool is not None:
                active.tool.hideTool()
        # set new active layer
        self.activeLayerIndex = stackIndex
        if self.layerView is not None:
            self.layerView.selectRow(lgStack - 1 - stackIndex)
        active = self.getActiveLayer()
        if active.tool is not None and active.visible:
            active.tool.showTool()
        return active

    def getActivePixel(self,x, y):
        """
        Reads pixel value from active layer. For
        adjustment or segmentation layers, we read the pixel value
        from the input image.
        @param x, y: coordinates of pixel, relative to full-sized image
        @return: pixel color
        @rtype: QColor
        """
        """
        currentImg = self.getCurrentImage()
        # get x, y coordinates relative to current image
        x = int((x * currentImg.width()) / self.width())
        y = int((y * currentImg.height()) / self.height())
        """
        x, y = self.full2CurrentXY(x, y)
        activeLayer = self.getActiveLayer()
        if activeLayer.isAdjustLayer() :
            # layer is adjustment or segmentation : read from current input image
            return activeLayer.inputImg().pixelColor(x, y)
        else:
            # read from current image
            return activeLayer.getCurrentImage().pixelColor(x, y)

    def cacheInvalidate(self):
        vImage.cacheInvalidate(self)
        for layer in self.layersStack:
            layer.cacheInvalidate()  # Qlayer doesn't inherit from mImage : it's a call to vImage.cacheInvalidate

    """
    def initThumb(self):
        vImage.initThumb(self)
        #for layer in self.layersStack:   03/10/17 prevent wrong reinit of the thumb stack when shifting from and to non color managed view in preview mode
            #vImage.initThumb(layer)      removed 12/11/17 : useless
    """

    def setThumbMode(self, value):
        if value == self.useThumb:
            return
        self.useThumb = value
        # recalculate the whole stack
        self.layerStack[0].apply()

    def updatePixmap(self):
        """
        Overrides vImage.updatePixmap()
        """
        vImage.updatePixmap(self)
        for layer in self.layersStack:
            vImage.updatePixmap(layer)

    def getStackIndex(self, layer):
        p = id(layer)
        i = -1
        for i,l in enumerate(self.layersStack):
            if id(l) == p:
                break
        return i

    def addLayer(self, layer, name='', index=None):
        """
        Adds layer.

        @param layer: layer to add (fresh layer if None, type QLayer)
        @type layer: QLayer
        @param name:
        @type name: str
        @param index: index of insertion in layersStack (top of active layer if index=None)
        @type index: int
        @return: the layer added
        @rtype: QLayer
        """
        # build a unique name
        usedNames = [l.name for l in self.layersStack]
        a = 1
        trialname = name if len(name) > 0 else 'noname'
        while trialname in usedNames:
            trialname = name + '_'+ str(a)
            a = a+1
        if layer is None:
            layer = QLayer(QImg=self)
            layer.fill(Qt.white)
        layer.name = trialname
        layer.parentImage = self
        if index==None:
            if self.activeLayerIndex is not None:
                # add on top of active layer if any
                index = self.activeLayerIndex
            else:
                # empty stack
                index = 0
        self.layersStack.insert(index, layer)
        self.setActiveLayer(index)
        layer.meta = self.meta
        layer.parentImage = self
        self.setModified(True)
        return layer

    def removeLayer(self, index=None):
        if index is None:
            return
        self.layersStack.pop(index)

    def addAdjustmentLayer(self, name='', role='',  index=None):
        """
        Add an adjustment layer to the layer stack, at
        position index (default is top of active layer)
        @param name: 
        @param index: 
        @return: 
        """
        if index == None:
            # adding on top of active layer
            index = self.activeLayerIndex
        # set image from active layer
        layer = QLayer.fromImage(self.layersStack[index], parentImage=self)
        layer.role = role
        self.addLayer(layer, name=name, index=index + 1)
        #layer.inputImg = lambda: self.layersStack[layer.getLowerVisibleStackIndex()].getCurrentMaskedImage()
        # init thumb
        if layer.parentImage.useThumb:
            layer.thumb = layer.inputImg().copy()  # TODO 15/11/17 addedc copy to prevent cycles in applyCloning, function qp.drawImage()
        group = self.layersStack[index].group
        if group:
            layer.group = group
            layer.mask = self.layersStack[index].mask
            layer.maskIsEnabled = True
        # sync caches
        layer.updatePixmap()
        return layer

    def addSegmentationLayer(self, name='', index=None):
        if index == None:
            index = self.activeLayerIndex
        layer = QLayer.fromImage(self.layersStack[index], parentImage=self)
        layer.role = 'SEGMENT'
        layer.inputImg = lambda: self.layersStack[layer.getLowerVisibleStackIndex()].getCurrentMaskedImage()
        self.addLayer(layer, name=name, index=index + 1)
        layer.updatePixmap()
        return layer

    def dupLayer(self, index=None):
        """
        inserts in layersStack at position index+1 a copy of the layer 
        at position index. If index is None (default value), the layer is inserted
        on top of the stack. Adjustment layers are not duplicated.
        @param index: 
        @type index: int
        @return: 
        """
        if index == None:
            index = len(self.layersStack) - 1
        layer0 = self.layersStack[index]
        if layer0.isAdjustLayer():
            return
        layer1 = QLayer.fromImage(layer0, parentImage=self)
        self.addLayer(layer1, name=layer0.name, index=index+1)

    def mergeVisibleLayers(self):
        """
        Merges the current visible masked images and returns the
        resulting QImage, eventually scaled to fit the image size.
        @return: image
        @rtype: QImage
        """
        # init new image
        img = QImage(self.width(), self.height(), self.format())
        img.fill(vImage.defaultBgColor)
        # draw layers and masks
        qp = QPainter(img)
        qp.drawImage(QRect(0, 0, self.width(), self.height()), self.layersStack[-1].getCurrentMaskedImage())
        qp.end()
        return img

    def save(self, filename, quality=-1, compression=-1):
        """
        Build image from visible layers
        and write it to file. Raise IOError if
        saving fails.
        @param filename:
        @type filename: str
        @param quality: integer value in range 0..100, or -1
        @type quality: int
        """
        # don't save thumbnails
        if self.useThumb:
            return False
        img = self.mergeVisibleLayers()
        # imagewriter and QImage.save are unusable for tif files,
        # due to bugs in libtiff.
        # We use opencv imwrite.
        fileFormat = filename[-3:].upper()
        if fileFormat == 'JPG':
            params = [cv2.IMWRITE_JPEG_QUALITY, quality]  # quality range 0..100
        elif fileFormat == 'PNG':
            params = [cv2.IMWRITE_PNG_COMPRESSION, compression]  # compression range 0..9
        elif fileFormat == 'TIF':
            params = []
        else:
            raise IOError("Invalid File Format\nValid formats are jpg, png, tif ")
        buf = QImageBuffer(img)[:,:,:3]
        written = cv2.imwrite(filename, buf, params)
        if not written:
            raise IOError("Cannot write file %s " % filename)
        self.setModified(False)

    def writeStackToStream(self, dataStream):
        dataStream.writeInt32(len(self.layersStack))
        for layer in self.layersStack:
            """
            dataStream.writeQString('menuLayer(None, "%s")' % layer.actionName)
            dataStream.writeQString('if "%s" != "actionNull":\n dataStream=window.label.img.layersStack[-1].readFromStream(dataStream)' % layer.actionName)
            """
            dataStream.writeQString(layer.actionName)
        for layer in self.layersStack:
            if hasattr(layer, 'view'):
                layer.view.widget().writeToStream(dataStream)

    def readStackFromStream(self, dataStream):
        # stack length
        count = dataStream.readInt32()
        script = []
        for i in range(count):
            actionName = dataStream.readQString()
            script.append('menuLayer(None, "%s")' % actionName)
            script.append('if "%s" != "actionNull":\n dataStream=window.label.img.layersStack[-1].readFromStream(dataStream)' % actionName)
        return script

    def saveStackToFile(self, filename):
        qf = QFile(filename)
        if not qf.open(QIODevice.WriteOnly):
            raise IOError('cannot open file %s' % filename)
        dataStream = QDataStream(qf)
        self.writeStackToStream(dataStream)
        qf.close()

    def loadStackFromFile(self, filename):
        qf = QFile(filename)
        if not qf.open(QIODevice.ReadOnly):
            raise IOError('cannot open file %s' % filename)
        dataStream = QDataStream(qf)
        script = self.readStackFromStream(dataStream)
        #qf.close()
        return script, qf, dataStream

class imImage(mImage) :
    """
    Interactive multi-layer image
    """
    def __init__(self, *args, **kwargs):
        super(imImage, self).__init__(*args, **kwargs)
        # Zoom_coeff = 1.0 displays an image fitting the
        # size of the current window ( NOT the actual pixels of the image).
        self.Zoom_coeff = 1.0
        self.xOffset, self.yOffset = 0, 0
        self.isMouseSelectable =True
        self.isModified = False

    def bTransformed(self, transformation):
        """
        Returns a copy of the transformed image and stack
        @param transformation:
        @type transformation: QTransform
        @return:
        @rtype: imImage
        """
        img = imImage(QImg=self.transformed(transformation))
        img.meta = self.meta
        img.onImageChanged = self.onImageChanged
        img.useThumb = self.useThumb
        img.useHald = self.useHald
        stack = []
        for layer in self.layersStack:
            tLayer = layer.bTransformed(transformation, img)
            #tLayer.name = tLayer.name + 'aaa'
            stack.append(tLayer)
        img.layersStack = stack
        gc.collect()
        return img

    def resize(self, pixels, interpolation=cv2.INTER_CUBIC):
        """
        Resize image and layers
        @param pixels:
        @param interpolation:
        @return: resized imImage object
        @rtype: imImage
        """
        # resized vImage
        rszd0 = super(imImage, self).resize(pixels, interpolation=interpolation)
        # resized imImage
        rszd = imImage(QImg=rszd0,meta=copy(self.meta))
        rszd.rect = rszd0.rect
        for k, l  in enumerate(self.layersStack):
            if l.name != "background" and l.name != 'drawlayer':
                img = QLayer.fromImage(l.resize(pixels, interpolation=interpolation), parentImage=rszd)
                rszd.layersStack.append(img)
                #rszd._layers[l.name] = img
        self.isModified = True
        return rszd

    def view(self):
        return self.Zoom_coeff, self.xOffset, self.yOffset

    def setView(self, zoom=1.0, xOffset=0.0, yOffset=0.0):
        """
        Sets viewing conditions: zoom, offset
        @param zoom: zoom coefficient
        @type zoom: float
        @param xOffset: x-offset
        @type xOffset: int
        @param yOffset: y-offset
        @type yOffset: int
        @return: 
        """
        self.Zoom_coeff, self.xOffset, self.yOffset = zoom, xOffset, yOffset

    def fit_window(self, win):
        """
        reset Zoom_coeff and offset
        @param win: 
        @type win:
        """
        self.Zoom_coeff = 1.0
        self.xOffset, self.yOffset = 0.0, 0.0

class QLayer(vImage):
    @classmethod
    def fromImage(cls, mImg, parentImage=None):
        layer = QLayer(QImg=mImg, parentImage=parentImage)
        layer.parentImage = parentImage
        return layer #QLayer(QImg=mImg) #mImg

    def __init__(self, *args, **kwargs):
        self.parentImage = kwargs.pop('parentImage', None)
        super(QLayer, self).__init__(*args, **kwargs)
        self.name='noname'
        self.visible = True
        self.isClipping = False
        self.role = kwargs.pop('role', '')
        self.tool = None
        # layer opacity is used by QPainter operations.
        # Its value must be in the range 0.0...1.0
        self.opacity = 1.0
        self.compositionMode = QPainter.CompositionMode_SourceOver
        # The next two attributes are used by adjustment layers only.
        # wrapper for the right exec method
        #self.execute = lambda l=None, pool=None: self.updatePixmap()
        self.execute = lambda l=None, pool=None: l.updatePixmap() if l is not None else None
        self.options = {}
        # Following attributes (functions)  are reserved (dynamic typing for adjustment layers) and set in addAdjustmentlayer() above
            # self.inputImg : access to upper lower visible layer image or thumbnail, according to flag useThumb
            # self.inputImgFull : access to upper lower visible layer image
            # Accessing upper lower thumbnail must be done by calling inputImgFull().thumb. Using inputImg().thumb will fail if useThumb is True.
        # actionName is used by methods graphics***.writeToStream()
        self.actionName = 'actionNull'
        # view is set by bLUe.menuLayer()
        self.view = None
        # containers are initialized (only once) by
        # getCurrentMaskedImage, their type is QLayer
        self.maskedImageContainer = None
        self.maskedThumbContainer = None
        # consecutive layers can be grouped.
        # A group is a list of QLayer objects
        self.group = []
        # layer offsets
        self.xOffset, self.yOffset = 0, 0
        self.Zoom_coeff = 1.0
        # layer AltOffsets are used by cloning
        # layers to shift an image clone.
        self.xAltOffset, self.yAltOffset = 0, 0
        self.AltZoom_coeff = 1.0

    def bTransformed(self, transformation, parentImage):
        """
        Applies transformation to a copy of layer and returns the copy.
        @param transformation:
        @type transformation: QTransform
        @param parentImage:
        @type parentImage: vImage
        @return: transformed layer
        @rtype: QLayer
        """
        tLayer = QLayer.fromImage(self.transformed(transformation), parentImage=parentImage)
        tLayer.name = self.name
        tLayer.actionName = self.actionName
        tLayer.view = self.view
        # cut link from old layer to graphic form
        # self.view = None                        # TODO 04/12/17 validate
        # link back grWindow to tLayer
        if tLayer.view is not None:
            tLayer.view.widget().layer = tLayer
        if hasattr(self, "clipLimit"):
            tLayer.clipLimit = self.clipLimit
        if hasattr(self, "temperature"):
            tLayer.temperature = self.temperature
        tLayer.execute = self.execute
        tLayer.mask = self.mask.transformed(transformation)
        return tLayer

    def bResized(self,w, h, parentImage):
        """
        resize a copy of layer
        @param w:
        @type w:
        @param h:
        @type h:
        @param parentImage:
        @type parentImage:
        @return:
        @rtype: Qlayer
        """
        transform = QTransform()
        transform = transform.scale(w / self.width(), h / self.height())
        return self.bTransformed(transform, parentImage)


    def initThumb(self):
        """
        Overrides vImage.initThumb, to set the parentImage attribute
        """
        """
        if not self.cachesEnabled:
            return
        """
        """
        scImg = self.scaled(self.thumbSize, self.thumbSize, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        # With the Qt.SmoothTransformation flag, the scaled image format is premultiplied
        self.thumb = scImg.convertToFormat(QImage.Format_ARGB32, Qt.DiffuseDither|Qt.DiffuseAlphaDither)
        """
        super().initThumb()
        self.thumb.parentImage = self.parentImage

    def initHald(self):
        """
        Builds a hald image (as a QImage) from identity 3D LUT.
        """
        if not self.cachesEnabled:
            return
        s = int((LUT3DIdentity.size) ** (3.0 / 2.0)) + 1
        buf0 = LUT3DIdentity.getHaldImage(s, s)
        #self.hald = QLayer(QImg=QImage(QSize(190,190), QImage.Format_ARGB32))
        self.hald = QImage(QSize(s, s), QImage.Format_ARGB32)
        buf1 = QImageBuffer(self.hald)
        buf1[:, :, :3] = buf0
        buf1[:,:,3] = 255
        self.hald.parentImage = self.parentImage

    def getHald(self):
        if not self.cachesEnabled:
            s = int((LUT3DIdentity.size) ** (3.0 / 2.0)) + 1
            buf0 = LUT3DIdentity.getHaldImage(s, s)
            # self.hald = QLayer(QImg=QImage(QSize(190,190), QImage.Format_ARGB32))
            hald = QImage(QSize(s, s), QImage.Format_ARGB32)
            buf1 = QImageBuffer(hald)
            buf1[:, :, :3] = buf0
            buf1[:, :, 3] = 255
            hald.parentImage = self.parentImage
            return hald
        if self.hald is None:
            self.initHald()
        return self.hald

    def getCurrentImage(self):
        """
        Returns current (full, preview or hald) image, according to
        the value of the flags useThumb and useHald. The thumbnail and hald
        are computed if they are not initialized.
        Otherwise, they are not updated unless self.thumb is
        None or purgeThumb is True.
        Overrides vImage method
        @return: current image
        @rtype: QLayer
        """
        if self.parentImage.useHald:
            return self.getHald()
        if self.parentImage.useThumb:
            return self.getThumb()
        else:
            return self

    def inputImg(self):
        return self.parentImage.layersStack[self.getLowerVisibleStackIndex()].getCurrentMaskedImage()

    def full2CurrentXY(self, x, y):
        """
        Maps x,y coordinates of pixel in the full image to
        coordinates in current image.
        @param x:
        @type x: int or float
        @param y:
        @type y: int or float
        @return:
        @rtype: 2uple of int
        """
        if self.parentImage.useThumb:
            currentImg = self.getThumb()
            x = (x * currentImg.width()) / self.width()
            y = (y * currentImg.height()) / self.height()
        return int(x), int(y)

    def getCurrentMaskedImage(self):
        """
        returns the current masked layer image, using the
        non color managed rPixmaps. For convenience, mainly
        to be able to use its color space buffers, the built image is
        of type QLayer. It is drawn on a container image, created only once.
        @return: masked image
        @rtype: QLayer
        """
        # init containers if needed
        if self.parentImage.useHald:
            return self.getHald()
        if self.maskedThumbContainer is None:
            self.maskedThumbContainer = QLayer.fromImage(self.getThumb(), parentImage=self.parentImage)
        if self.maskedImageContainer is None:
            self.maskedImageContainer = QLayer.fromImage(self, parentImage=self.parentImage)
        if self.parentImage.useThumb:
            img = self.maskedThumbContainer
        else:
            img = self.maskedImageContainer
        # no thumbnails for containers
        img.getThumb = lambda: img
        # draw lower stack
        qp = QPainter(img)
        top = self.parentImage.getStackIndex(self)
        # for adjustment layers, we must choose
        #      1) apply transformation to all lower layers : we draw
        #         the stack from 0 to top
        #      2) apply transformation to next lower layer alone : we draw
        #         the top layer only
        if self.isClipping:
            bottom = top
            qp.setCompositionMode(QPainter.CompositionMode_Source)
            qp.drawImage(QRect(0, 0, img.width(), img.height()), checkeredImage(img.width(), img.height()))
        else:
            bottom = 0
        for i, layer in enumerate(self.parentImage.layersStack[bottom:top+1]):
            if layer.visible:
                if i == 0:
                    qp.setCompositionMode(QPainter.CompositionMode_Source)
                else:
                    qp.setOpacity(layer.opacity)
                    qp.setCompositionMode(layer.compositionMode)
                if layer.rPixmap is not None:
                    qp.drawPixmap(QRect(0,0,img.width(), img.height()), layer.rPixmap)
                else:
                    qp.drawImage(QRect(0,0,img.width(), img.height()), layer.getCurrentImage())
        qp.end()
        return img

    def getHspbBuffer(self):
        """
        Return the image buffer in color mode HSpB.
        The buffer is calculated if needed and cached.
        @return: HSPB buffer
        @rtype: ndarray, dtype numpy float
        """
        if self.hspbBuffer is None or not self.cachesEnabled:
            img = self.getCurrentImage()
            self.hspbBuffer = rgb2hspVec(QImageBuffer(img)[:, :, :3][:, :, ::-1])
        return self.hspbBuffer

    def getLabBuffer(self):
        """
        returns the image buffer in color mode Lab.
        The buffer is calculated if needed and cached.
        @return: Lab buffer (type ndarray)
        """
        """
        if self.LabBuffer is None:
            if self.parentImage.useThumb:
                #self.LabBuffer = sRGB2LabVec(QImageBuffer(self.inputImgFull().getCurrentImage())[:, :, :3][:, :, ::-1])
                #self.LabBuffer = sRGB2LabVec(QImageBuffer(self.inputImg())[:, :, :3][:, :, ::-1])  # TODO verify getcurrenmaskedimage is right here
                self.LabBuffer = sRGB2LabVec(QImageBuffer(self.getCurrentImage())[:, :, :3][:, :, ::-1])
            else:
                if hasattr(self, 'inputImg'):
                    self.LabBuffer = sRGB2LabVec(QImageBuffer(self.inputImg())[:,:,:3][:,:,::-1])
                else:
                    self.LabBuffer = sRGB2LabVec(QImageBuffer(self)[:, :, :3][:, :, ::-1])
        """
        if self.LabBuffer is None or not self.cachesEnabled:
            #self.thumb = None
            # Calling QImageBuffer  needs to keep a ref to self.getCurrentImage() to protect it against garbage collector.
            img = self.getCurrentImage()
            self.LabBuffer = sRGB2LabVec(QImageBuffer(img)[:, :, :3][:, :, ::-1])
        return self.LabBuffer

    def applyToStack(self):
        """
        Applies transformation and propagates changes to upper layers.
        """
        # recursive function
        def applyToStack_(layer, pool=None):
            # apply Transformation (call vImage.apply*LUT...)
            if layer.visible:
                start = time()
                layer.execute(l=layer, pool=pool)
                layer.cacheInvalidate()
                print("%s %.2f" %(layer.name, time()-start))
            stack = layer.parentImage.layersStack
            ind = layer.getStackIndex() + 1
            # get next visible upper layer
            while ind < len(stack):
                if stack[ind].visible:
                    break
                ind += 1
            if ind < len(stack):
                layer1 = stack[ind]
                applyToStack_(layer1, pool=pool)
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            QApplication.processEvents()
            if (not self.parentImage.useThumb or self.parentImage.useHald):
                pool = None
                # pool = multiprocessing.Pool(MULTIPROC_POOLSIZE)  # TODO time opt : pool is always created and used only by apply3DLUT; time 0.3s
            else:
                pool = None
            applyToStack_(self, pool=pool)
            if pool is not None:
                pool.close()
            pool = None
        finally:
            self.parentImage.setModified(True)  #TODO 28/02/18 validate
            QApplication.restoreOverrideCursor()
            QApplication.processEvents()

    def applyToStackIter(self):
        """
        iterative version of applyToStack
        @return:
        """
        stack = self.parentImage.layersStack
        ind = self.getStackIndex() + 1
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            QApplication.processEvents()
            self.execute()
            for layer in stack[ind:]:
                if layer.visible:
                    layer.cacheInvalidate()
                    # for hald friendly layer compute output hald, otherwise compute output image
                    layer.execute()
        finally:
            QApplication.restoreOverrideCursor()
            QApplication.processEvents()


    def isAdjustLayer(self):
        return self.view is not None #hasattr(self, 'view')

    def isSegmentLayer(self):
        return 'SEGMENT' in self.role

    def isCloningLayer(self):
        return 'CLONING' in self.role

    def isGeomLayer(self):
        return 'GEOM' in self.role

    def is3DLUTLayer(self):
        return '3DLUT' in self.role

    def updatePixmap(self, maskOnly = False):
        """
        Updates the caches qPixmap, rPixmap and cmImage.
        The input image is that returned by getCurrentImage(), thus
        the caches are synchronized using the current image
        mode (full or preview).
        If maskOnly is True, cmImage is not updated.
        if maskIsEnabled is False, the mask is not shown.
        If maskIsEnabled is True, then
            - if maskIsSelected is True, the mask is drawn over
              the layer as a color mask.
            - if maskIsSelected is False, the mask is drawn as an
              opacity mask, setting image opacity to that of mask
              (mode DestinationIn). Mask color is no used.
        @param maskOnly: default False
        @type maskOnly: boolean
        """
        currentImage = self.getCurrentImage()
        if not maskOnly:
            # invalidate color managed cache
            self.cmImage = None
        # get (eventually) up to date  color managed image
        if icc.COLOR_MANAGE and self.parentImage is not None:
            # layer color model is parent image color model
            if self.cmImage is None:
                # CAUTION : reset alpha channel
                img = convertQImage(currentImage, transformation=self.parentImage.colorTransformation)  # time 0.7 s for full res.
                # restore alpha
                buf0 = QImageBuffer(img)
                buf1 = QImageBuffer(currentImage)
                buf0[:,:,3] = buf1[:,:,3]
            else:
                img = self.cmImage
        else:
            img = currentImage
        #if maskOnly:
        self.cmImage = img
        qImg = img
        rImg = currentImage
        # apply layer transformation. Missing pixels are set to QColor(0,0,0,0)
        if self.xOffset != 0 or self.yOffset != 0:
            x,y = self.full2CurrentXY(self.xOffset, self.yOffset)
            qImg = qImg.copy(QRect(-x, -y, qImg.width()*self.Zoom_coeff, qImg.height()*self.Zoom_coeff))
            rImg = rImg.copy(QRect(-x, -y, rImg.width()*self.Zoom_coeff, rImg.height()*self.Zoom_coeff))
        if self.maskIsEnabled:
            qImg = vImage.visualizeMask(qImg, self.mask, color=self.maskIsSelected, clipping=self.isClipping)
            rImg = vImage.visualizeMask(rImg, self.mask, color=self.maskIsSelected, clipping=self.isClipping)
        self.qPixmap = QPixmap.fromImage(qImg)
        self.rPixmap = QPixmap.fromImage(rImg)
        self.setModified(True)

    def getStackIndex(self):
        for i, l in enumerate(self.parentImage.layersStack):
            if l is self:
                break
        return i

    def updateTableView(self, table):
        """
        refreshes the corresponding row in table
        @param table:
        @type table: QTableView
        """
        ind = self.getStackIndex()
        table.updateRow(len(self.parentImage.layersStack) - 1 - ind)

    def getLowerVisibleStackIndex(self):
        """
        Return the index of the next lower visible layer,
        -1 if it does not exists
        @return:
        @rtype: int
        """
        ind = self.getStackIndex()
        for i in range(ind-1, -1, -1):
            if self.parentImage.layersStack[i].visible:
                return i
        return -1

    def getUpperVisibleStackIndex(self):
        """
        Return the index of the next upper visible layer,
        -1 if it does not exists
        @return:
        @rtype:
        """
        ind = self.getStackIndex()
        for i in range(ind+1, len(self.parentImage.layersStack), 1):
            if self.parentImage.layersStack[i].visible:
                return i
        return -1


    def linkMask2Lower(self):
        """
        share mask with next lower layer
        @return:
        @rtype:
        """
        ind = self.getStackIndex()
        if ind == 0:
            return
        lower = self.parentImage.layersStack[ind-1]
        # don't link two groups
        if self.group and lower.group:
            return
        if not self.group and not lower.group:
            self.group = [self, lower]
            lower.group = self.group
        elif not lower.group :
            if not any(o is lower for o in self.group):
                self.group.append(lower)
            lower.group  = self.group
        elif not self.group:
            if not any(item is self for item in lower.group):
                lower.group.append(self)
            self.group = lower.group
        self.mask = lower.mask

    def unlinkMask(self):
        self.mask =self.mask.copy()
        # remove self from group
        for i,item in enumerate(self.group):
            if item is self:
                self.group.pop(i)
                # don't keep  group with length 1
                if len(self.group) == 1:
                    self.group.pop(0)
                break
        self.group = []


    def merge_with_layer_immediately_below(self):
        """
        Merges a layer with the next lower visible layer. Does nothing
        if mode is preview or the target layer is an adjustment layer.
        @return:
        @rtype:
        """
        if not hasattr(self, 'inputImg'):
            return
        ind = self.getLowerVisibleStackIndex()
        if ind < 0:
            # no visible layer found
            return
        target = self.parentImage.layersStack[ind]
        if hasattr(target, 'inputImg') or self.parentImage.useThumb:
            msgBox = QMessageBox()
            msgBox.setText("Cannot Merge layers")
            msgBox.setInformativeText("Uncheck Preview first" if self.parentImage.useThumb else "Target layer must be background or image" )
            msgBox.exec()
            return
        #update stack
        self.parentImage.layersStack[0].applyToStack()
        # merge
        #target.setImage(self)
        qp = QPainter(target)
        qp.setCompositionMode(self.compositionMode)
        qp.setOpacity(self.opacity)
        qp.drawImage(QRect(0,0,self.width(), self.height()), self)
        target.updatePixmap()

        self.parentImage.layerView.clear(delete=False)
        currentIndex = self.getStackIndex()
        self.parentImage.activeLayerIndex = ind
        self.parentImage.layersStack.pop(currentIndex)
        self.parentImage.layerView.setLayers(self.parentImage)

    def reset(self):
        """
        reset layer to inputImg
        @return:
        """
        self.setImage(self.inputImg())

    def setOpacity(self, value):
        """
        set the opacity attribute to value/100.0
        @param value:
        """
        self.opacity = value /100.0
        return

    def readFromStream(self, dataStream):

        if hasattr(self, 'view'):
            self.view.widget().readFromStream(dataStream)
        return dataStream

def apply3DLUTSliceCls(LUT, inputBuffer, imgBuffer, s ):

    inputBuffer = inputBuffer[s[1], s[0], :]
    imgBuffer = imgBuffer[:, :, :]
    ndImg0 = inputBuffer[:, :, :3]
    ndImg1 = imgBuffer[:, :, :3]
    # apply LUT
    start = time()
    ndImg1[s[1], s[0], :] = interpVec_(LUT, ndImg0)
    end = time()
    #print 'Apply3DLUT time %.2f' % (end - start)


def applyHaldCls(item):
    """
    Transforms a hald image into a 3DLUT object and applies
    the 3D LUT to the current view of self.
    @param hald: hald image
    @type hald: QImage
    """
    # QImageBuffer(l.hald), QImageBuffer(l.inputImg()), QImageBuffer(l.getCurrentImage()), s
    lut = LUT3D.HaldBuffer2LUT3D(item[0])
    apply3DLUTSliceCls(lut.LUT3DArray, item[1], item[2], item[3])
    return item[2]
    #img.apply3DLUT(lut.LUT3DArray, options={'use selection' : True})




