import io
import shutil
from pathlib import Path
import pytest
from PIL import Image, ImageDraw, ImageFont
from coverly.intelligence import run_ocr, ocr_capabilities


def test_tamil_english_pipeline_calls_real_interface(tmp_path, monkeypatch):
    monkeypatch.setattr('coverly.intelligence.ocr_capabilities',lambda:{'available':True,'languages':['tam','eng']})
    calls=[]
    def ocr(image,**kwargs):
        calls.append(kwargs)
        return {'text':['நாயகன்','Nayakan'],'conf':['92','95']}
    monkeypatch.setattr('pytesseract.image_to_data',ocr)
    path=tmp_path/'ocr.jpg'
    Image.new('RGB',(600,600),'white').save(path)
    text,confidence=run_ocr(path,'tam+eng')
    assert 'நாயகன்' in text and 'Nayakan' in text
    assert confidence > .9
    assert len(calls)==3 and all(c['lang']=='tam+eng' for c in calls)


def test_missing_tamil_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr('coverly.intelligence.ocr_capabilities',lambda:{'available':True,'languages':['eng']})
    with pytest.raises(RuntimeError,match='tam'):
        run_ocr(tmp_path/'missing.jpg','tam+eng')


def test_live_tamil_ocr_when_installed(tmp_path):
    caps=ocr_capabilities()
    if not caps['available'] or 'tam' not in caps['languages']:
        pytest.skip('Tesseract Tamil data is not installed on this host; Linux OCR CI installs it')
    fonts=[Path('/usr/share/fonts/truetype/noto/NotoSansTamil-Regular.ttf'),Path('/System/Library/Fonts/Supplemental/Tamil MN.ttc')]
    font=next((p for p in fonts if p.is_file()),None)
    if font is None:
        pytest.skip('Tamil font required for controlled live OCR fixture')
    image=Image.new('RGB',(1600,500),'white')
    draw=ImageDraw.Draw(image)
    draw.text((100,150),'நாயகன்',font=ImageFont.truetype(str(font),120),fill='black')
    path=tmp_path/'tamil.png'
    image.save(path)
    text,confidence=run_ocr(path,'tam+eng')
    assert 'நாயகன்' in text
