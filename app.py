from flask import Flask, render_template, request, jsonify
import pdfplumber
import re
from datetime import datetime
from werkzeug.utils import secure_filename
import os

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['UPLOAD_FOLDER'] = 'uploads'

# Create uploads folder if it doesn't exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

def extract_pdf_text(pdf_file):
    """Extract text from PDF file"""
    try:
        text = ""
        with pdfplumber.open(pdf_file) as pdf:
            for page in pdf.pages:
                text += page.extract_text() + "\n"
        return text
    except Exception as e:
        return None

def parse_shipper_data(text):
    """
    Parse GCRS shipper PDF to extract control numbers, dates, part numbers, qty, amount
    Expected format: control number, part number, quantity, dollar amount per line
    """
    lines = []
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        parts = re.split(r'\s+', line)
        if len(parts) >= 4:
            try:
                control_num = parts[0]
                part_num = parts[1] if len(parts) > 1 else ""
                qty = parts[2] if len(parts) > 2 else "0"
                amount = parts[3] if len(parts) > 3 else "0"
                if any(char.isdigit() for char in control_num):
                    lines.append({
                        'control': control_num,
                        'part': part_num,
                        'qty': qty,
                        'amount': amount,
                        'raw': line
                    })
            except:
                continue
    return lines

def parse_credit_data(text):
    """
    Parse core return credit PDF to extract control numbers
    """
    controls = set()
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        parts = re.split(r'\s+', line)
        if len(parts) >= 1:
            control_num = parts[0]
            if any(char.isdigit() for char in control_num):
                controls.add(control_num)
    return controls

def match_and_find_unclaimed(shipper_data, credited_controls):
    """
    Compare shipper data with credited controls
    Return lines where control number is NOT in credited controls
    """
    unclaimed = []
    for item in shipper_data:
        if item['control'] not in credited_controls:
            unclaimed.append(item)
    return unclaimed

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/check-cores', methods=['POST'])
def check_cores():
    """
    API endpoint to process uploaded PDFs and find unclaimed cores
    """
    try:
        if 'shipper_pdf' not in request.files or 'credit_pdf' not in request.files:
            return jsonify({'error': 'Both PDF files are required'}), 400
        shipper_file = request.files['shipper_pdf']
        credit_file = request.files['credit_pdf']
        if shipper_file.filename == '' or credit_file.filename == '':
            return jsonify({'error': 'Both files must be selected'}), 400
        shipper_filename = secure_filename(shipper_file.filename)
        shipper_path = os.path.join(app.config['UPLOAD_FOLDER'], shipper_filename)
        shipper_file.save(shipper_path)
        shipper_text = extract_pdf_text(shipper_path)
        if not shipper_text:
            return jsonify({'error': 'Could not read shipper PDF'}), 400
        credit_filename = secure_filename(credit_file.filename)
        credit_path = os.path.join(app.config['UPLOAD_FOLDER'], credit_filename)
        credit_file.save(credit_path)
        credit_text = extract_pdf_text(credit_path)
        if not credit_text:
            return jsonify({'error': 'Could not read credit memo PDF'}), 400
        shipper_data = parse_shipper_data(shipper_text)
        credited_controls = parse_credit_data(credit_text)
        unclaimed = match_and_find_unclaimed(shipper_data, credited_controls)
        total_amount = 0
        for item in unclaimed:
            try:
                amount = float(item['amount'].replace('$', '').replace(',', ''))
                total_amount += amount
            except:
                pass
        unclaimed_sorted = sorted(unclaimed, key=lambda x: x['control'])
        try:
            os.remove(shipper_path)
            os.remove(credit_path)
        except:
            pass
        return jsonify({
            'success': True,
            'unclaimed_count': len(unclaimed),
            'total_amount': round(total_amount, 2),
            'results': unclaimed_sorted[:50]
        })
    except Exception as e:
        return jsonify({'error': f'Error processing files: {str(e)}'}), 500

if __name__ == '__main__':
    app.run(debug=True)
