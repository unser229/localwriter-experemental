import zipfile
from lxml import etree

def extract_xml_features(docx_path):
    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    features = []
    
    with zipfile.ZipFile(docx_path) as z:
        xml_content = z.read('word/document.xml')
        tree = etree.fromstring(xml_content)
        
        for p in tree.xpath('//w:p', namespaces=ns):
            # 1. Свойства абзаца (Paragraph Properties)
            pPr = p.find('w:pPr', namespaces=ns)
            style = pPr.find('w:pStyle', namespaces=ns).get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val') if pPr is not None and pPr.find('w:pStyle', namespaces=ns) is not None else "Normal"
            align = pPr.find('w:jc', namespaces=ns).get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val') if pPr is not None and pPr.find('w:jc', namespaces=ns) is not None else "left"
            
            # 2. Свойства текста (Run Properties) - берем первый прогон для примера
            r = p.xpath('.//w:r', namespaces=ns)
            full_text = "".join([t.text for t in p.xpath('.//w:t', namespaces=ns) if t.text])
            
            if r:
                rPr = r[0].find('w:rPr', namespaces=ns)
                is_bold = rPr.find('w:b', namespaces=ns) is not None
                font_size = rPr.find('w:sz', namespaces=ns).get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val') if rPr is not None and rPr.find('w:sz', namespaces=ns) is not None else "24" # 24 = 12pt
            else:
                is_bold, font_size = False, "24"

            features.append({
                "text": full_text,
                "xml_style": style,
                "alignment": align,
                "is_bold": is_bold,
                "font_size_half_pt": int(font_size), # В XML размер в полупунктах
                "is_heading": "Heading" in style
            })
    return features