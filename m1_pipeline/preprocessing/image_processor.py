from typing import Union
import cv2
import numpy as np
from PIL import Image


def preprocess_image(image: Union[Image.Image, np.ndarray]) -> np.ndarray:
    """
    Applica la pipeline completa di pre-elaborazione a un'immagine.

    Args:
        image: Immagine PIL o array numpy (RGB o grayscale).

    Returns:
        Immagine binarizzata come array numpy uint8.
    """
    img = _to_numpy(image)
    gray = _to_grayscale(img)
    denoised = _denoise(gray)
    deskewed = _deskew(denoised)
    binary = _binarize(deskewed)
    return binary


# ---------------------------------------------------------------------------
# Funzioni interne
# ---------------------------------------------------------------------------

def _to_numpy(image: Union[Image.Image, np.ndarray]) -> np.ndarray:
    if isinstance(image, Image.Image):
        return np.array(image)
    return image.copy()


def _to_grayscale(img: np.ndarray) -> np.ndarray:
    if len(img.shape) == 2:
        return img
    if img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_RGBA2GRAY)
    else:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    return img


def _denoise(gray: np.ndarray) -> np.ndarray:
    """Rimuove rumore con fastNlMeansDenoising."""
    return cv2.fastNlMeansDenoising(gray, h=10, templateWindowSize=7, searchWindowSize=21)


def _deskew(gray: np.ndarray) -> np.ndarray:
    """
    Corregge l'inclinazione del documento.
    Usa minAreaRect sui pixel scuri per stimare l'angolo di rotazione.
    """
    # Threshold provvisorio per trovare pixel scuri
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    coords = np.column_stack(np.where(thresh > 0))

    if len(coords) < 10:
        return gray  # immagine troppo vuota, skip

    angle = cv2.minAreaRect(coords)[-1]

    # Normalizza l'angolo nell'intervallo (-45, 45]
    if angle < -45:
        angle = 90 + angle
    else:
        angle = -angle

    if abs(angle) < 0.3:  # inclinazione trascurabile
        return gray

    (h, w) = gray.shape
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(
        gray, M, (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return rotated


def _binarize(gray: np.ndarray) -> np.ndarray:
    """
    Binarizzazione adattiva: usa Otsu globale + soglia adattiva locale.
    Il risultato finale è testo nero su sfondo bianco.
    """
    # Otsu globale
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Soglia adattiva (migliora su illuminazione non uniforme)
    adaptive = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=31,
        C=10,
    )

    # Combina: pixel bianco solo se bianco in entrambi
    combined = cv2.bitwise_and(otsu, adaptive)
    return combined
