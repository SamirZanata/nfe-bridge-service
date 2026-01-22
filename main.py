from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse
import uvicorn
from decouple import config
from pynfe.processamento.comunicacao import ComunicacaoSefaz
import os
import shutil
from pathlib import Path
from typing import Optional
from lxml import etree
import requests
import base64
import gzip
from pydantic import BaseModel

app = FastAPI(title="QuickSign SefazBridge")

# Diretório para armazenar certificados enviados
CERTIFICATES_DIR = Path("certificates")
CERTIFICATES_DIR.mkdir(exist_ok=True)

# Armazenamento em memória das configurações do certificado
# Em produção, considere usar um banco de dados ou arquivo de configuração seguro
certificate_config = {
    "path": None,
    "password": None,
    "uf": None,
    "homologacao": False
}

# Carregando configurações do .env (fallback)
CERT_PATH = config('CERT_PATH', default='certificado.pfx')
CERT_PASS = config('CERT_PASSWORD', default='')
# Remove comentários inline da UF
UF_RAW = config('UF', default='MG')
UF = UF_RAW.split('#')[0].strip() if UF_RAW else 'MG'  # Remove comentários
HOMOLOGACAO = config('HOMOLOGACAO', default=False, cast=bool)

# Mapeamento de códigos UF (IBGE) para siglas
CODIGOS_UF = {
    '11': 'RO', '12': 'AC', '13': 'AM', '14': 'RR', '15': 'PA',
    '16': 'AP', '17': 'TO', '21': 'MA', '22': 'PI', '23': 'CE',
    '24': 'RN', '25': 'PB', '26': 'PE', '27': 'AL', '28': 'SE',
    '29': 'BA', '31': 'MG', '32': 'ES', '33': 'RJ', '35': 'SP',
    '41': 'PR', '42': 'SC', '43': 'RS', '50': 'MS', '51': 'MT',
    '52': 'GO', '53': 'DF'
}

def detect_uf_from_key(nfe_key: str) -> str:
    """Detecta a UF a partir dos dois primeiros dígitos da chave de acesso"""
    if len(nfe_key) < 2:
        return None
    codigo_uf = nfe_key[:2]
    return CODIGOS_UF.get(codigo_uf)

def extract_nfe_data_from_xml(xml_content: str) -> dict:
    """Extrai dados da NF-e a partir do XML completo (já descompactado do docZip)"""
    try:
        # Faz parsing do XML
        if isinstance(xml_content, str):
            # Remove BOM se presente
            if xml_content.startswith('\ufeff'):
                xml_content = xml_content[1:]
            
            # Tenta fazer parsing
            try:
                root = etree.fromstring(xml_content.encode('utf-8'))
            except:
                # Se falhar, tenta sem encoding explícito
                root = etree.fromstring(xml_content)
        else:
            root = xml_content
        
        # Namespace padrão da NF-e - tenta descobrir o namespace do XML
        ns_uri = None
        if '}' in root.tag:
            ns_uri = root.tag.split('}')[0][1:]
        else:
            # Tenta encontrar o namespace em qualquer elemento
            for elem in root.iter():
                if '}' in elem.tag:
                    ns_uri = elem.tag.split('}')[0][1:]
                    break
        
        # Se não encontrou namespace, usa o padrão
        if not ns_uri:
            ns_uri = 'http://www.portalfiscal.inf.br/nfe'
        
        ns = {'ns': ns_uri}
        
        # Tenta usar xpath primeiro (mais eficiente)
        try:
            # Extrai nome do destinatário usando xpath
            customer_name_nodes = root.xpath('//ns:dest/ns:xNome/text()', namespaces=ns)
            if not customer_name_nodes:
                # Tenta sem namespace
                customer_name_nodes = root.xpath('//dest/xNome/text()')
            customer_name = customer_name_nodes[0].strip() if customer_name_nodes else None
            
            # Extrai CPF/CNPJ
            tax_id_nodes = root.xpath('//ns:dest/ns:CPF/text() | //ns:dest/ns:CNPJ/text()', namespaces=ns)
            if not tax_id_nodes:
                # Tenta sem namespace
                tax_id_nodes = root.xpath('//dest/CPF/text() | //dest/CNPJ/text()')
            tax_id_raw = tax_id_nodes[0].strip() if tax_id_nodes else None
            
            # Extrai endereço usando xpath
            xlgr_nodes = root.xpath('//ns:dest/ns:enderDest/ns:xLgr/text()', namespaces=ns)
            if not xlgr_nodes:
                xlgr_nodes = root.xpath('//dest/enderDest/xLgr/text()')
            
            nro_nodes = root.xpath('//ns:dest/ns:enderDest/ns:nro/text()', namespaces=ns)
            if not nro_nodes:
                nro_nodes = root.xpath('//dest/enderDest/nro/text()')
            
            xcpl_nodes = root.xpath('//ns:dest/ns:enderDest/ns:xCpl/text()', namespaces=ns)
            if not xcpl_nodes:
                xcpl_nodes = root.xpath('//dest/enderDest/xCpl/text()')
            
            xbairro_nodes = root.xpath('//ns:dest/ns:enderDest/ns:xBairro/text()', namespaces=ns)
            if not xbairro_nodes:
                xbairro_nodes = root.xpath('//dest/enderDest/xBairro/text()')
            
            xmun_nodes = root.xpath('//ns:dest/ns:enderDest/ns:xMun/text()', namespaces=ns)
            if not xmun_nodes:
                xmun_nodes = root.xpath('//dest/enderDest/xMun/text()')
            
            uf_nodes = root.xpath('//ns:dest/ns:enderDest/ns:UF/text()', namespaces=ns)
            if not uf_nodes:
                uf_nodes = root.xpath('//dest/enderDest/UF/text()')
            
            # Monta o endereço
            partes_endereco = []
            if xlgr_nodes:
                partes_endereco.append(xlgr_nodes[0].strip())
            if nro_nodes:
                partes_endereco.append(f", {nro_nodes[0].strip()}")
            if xcpl_nodes:
                partes_endereco.append(f" - {xcpl_nodes[0].strip()}")
            if xbairro_nodes:
                partes_endereco.append(f" - {xbairro_nodes[0].strip()}")
            if xmun_nodes:
                cidade = xmun_nodes[0].strip()
                if uf_nodes:
                    cidade += f"/{uf_nodes[0].strip()}"
                partes_endereco.append(f" - {cidade}")
            elif uf_nodes:
                partes_endereco.append(f" - {uf_nodes[0].strip()}")
            
            customer_address = ''.join(partes_endereco) if partes_endereco else None
            
            # Formata CPF/CNPJ
            tax_id = None
            if tax_id_raw:
                # Remove caracteres não numéricos
                doc_text = ''.join(filter(str.isdigit, tax_id_raw))
                if len(doc_text) == 14:
                    # Formata CNPJ: XX.XXX.XXX/XXXX-XX
                    tax_id = f"{doc_text[:2]}.{doc_text[2:5]}.{doc_text[5:8]}/{doc_text[8:12]}-{doc_text[12:]}"
                elif len(doc_text) == 11:
                    # Formata CPF: XXX.XXX.XXX-XX
                    tax_id = f"{doc_text[:3]}.{doc_text[3:6]}.{doc_text[6:9]}-{doc_text[9:]}"
            
            # Se conseguiu extrair pelo menos o nome, retorna os dados
            if customer_name:
                return {
                    "customer_name": customer_name,
                    "customer_address": customer_address or "Endereço não encontrado no XML",
                    "tax_id": tax_id or "00.000.000/0000-00"
                }
        except Exception as xpath_error:
            # Se xpath falhar, usa o método de busca tradicional
            pass
        
        # Identifica a tag raiz e ajusta para buscar no lugar certo
        root_tag = root.tag.split('}')[-1] if '}' in root.tag else root.tag
        
        # Se o root é nfeProc, busca o infNFe dentro dele
        if root_tag == 'nfeProc':
            # Procura pelo infNFe dentro de nfeProc > NFe > infNFe
            inf_nfe = None
            for elem in root.iter():
                tag_local = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                if tag_local == 'infNFe':
                    inf_nfe = elem
                    break
            if inf_nfe is not None:
                root = inf_nfe
        elif root_tag == 'NFe':
            # Se o root é NFe, busca o infNFe dentro dele
            inf_nfe = None
            for elem in root.iter():
                tag_local = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                if tag_local == 'infNFe':
                    inf_nfe = elem
                    break
            if inf_nfe is not None:
                root = inf_nfe
        elif root_tag != 'infNFe':
            # Se não é nenhum dos acima, procura pelo infNFe em qualquer lugar
            inf_nfe = None
            for elem in root.iter():
                tag_local = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                if tag_local == 'infNFe':
                    inf_nfe = elem
                    break
            if inf_nfe is not None:
                root = inf_nfe
        
        # Função auxiliar para buscar elemento ignorando namespace
        def find_element_any_ns(parent, tag_name):
            """Busca elemento ignorando namespace"""
            # Tenta com namespace explícito
            for ns_uri in ['http://www.portalfiscal.inf.br/nfe', '']:
                try:
                    elem = parent.find(f'.//{{{ns_uri}}}{tag_name}' if ns_uri else f'.//{tag_name}')
                    if elem is not None:
                        return elem
                except:
                    pass
            
            # Tenta buscar por tag sem namespace (qualquer namespace)
            for elem in parent.iter():
                tag_local = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                if tag_local == tag_name:
                    return elem
            return None
        
        # Função auxiliar para buscar todos os elementos com uma tag
        def findall_elements_any_ns(parent, tag_name):
            """Busca todos os elementos com a tag, ignorando namespace"""
            results = []
            for elem in parent.iter():
                tag_local = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                if tag_local == tag_name:
                    results.append(elem)
            return results
        
        # Extrai dados do destinatário (cliente)
        # Procura em diferentes locais do XML
        destinatario = None
        
        # Busca o elemento dest diretamente (já estamos no lugar certo ou vamos buscar)
        # Primeiro tenta buscar diretamente no root atual
        destinatario = find_element_any_ns(root, 'dest')
        
        # Se não encontrou, tenta diferentes caminhos
        if destinatario is None:
            search_paths = [
                ('infNFe', 'dest'),  # Dentro de infNFe
                ('NFe', 'infNFe', 'dest'),  # Dentro de NFe/infNFe
                ('nfeProc', 'NFe', 'infNFe', 'dest'),  # Dentro de nfeProc/NFe/infNFe
            ]
            
            # Se o root atual não é infNFe, precisa buscar de forma diferente
            current_root = root
            root_tag_current = current_root.tag.split('}')[-1] if '}' in current_root.tag else current_root.tag
            
            # Se não estamos em infNFe, volta para o root original para buscar
            if root_tag_current != 'infNFe':
                # Volta para o root original do XML
                try:
                    if isinstance(xml_content, str):
                        original_root = etree.fromstring(xml_content.encode('utf-8'))
                    else:
                        original_root = etree.fromstring(str(xml_content).encode('utf-8'))
                    current_root = original_root
                except:
                    current_root = root
            
            for path_parts in search_paths:
                current = current_root
                found = True
                for part in path_parts:
                    if part:
                        current = find_element_any_ns(current, part)
                        if current is None:
                            found = False
                            break
                if found and current is not None:
                    destinatario = current
                    break
        
        # Se não encontrou pelo caminho, busca diretamente por iteração
        if destinatario is None:
            # Busca recursiva em todo o XML pelo elemento dest
            def find_dest_recursive(elem):
                tag_local = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                if tag_local == 'dest':
                    return elem
                for child in elem:
                    result = find_dest_recursive(child)
                    if result is not None:
                        return result
                return None
            
            destinatario = find_dest_recursive(root)
            
            # Se ainda não encontrou, tenta buscar diretamente nos filhos do root
            if destinatario is None:
                for child in root:
                    tag_local = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                    if tag_local == 'dest':
                        destinatario = child
                        break
        
        # Extrai nome do cliente
        customer_name = None
        if destinatario is not None:
            # Tenta diferentes tags para o nome (xNome é o padrão na NF-e)
            for tag in ['xNome', 'xNomeDest', 'nome', 'xNomeDestinatario']:
                nome_elem = find_element_any_ns(destinatario, tag)
                if nome_elem is not None and nome_elem.text:
                    customer_name = nome_elem.text.strip()
                    break
            
            # Se não encontrou, tenta buscar diretamente nos filhos
            if not customer_name:
                for child in destinatario:
                    tag_local = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                    if tag_local in ['xNome', 'xNomeDest', 'nome'] and child.text:
                        customer_name = child.text.strip()
                        break
        
        # Extrai CNPJ/CPF do cliente
        tax_id = None
        if destinatario is not None:
            # Tenta buscar CNPJ primeiro, depois CPF
            for tag in ['CNPJ', 'CPF']:
                doc_elem = find_element_any_ns(destinatario, tag)
                if doc_elem is not None and doc_elem.text:
                    doc_text = doc_elem.text.strip()
                    # Remove caracteres não numéricos
                    doc_text = ''.join(filter(str.isdigit, doc_text))
                    # Valida se parece com CNPJ (14 dígitos) ou CPF (11 dígitos)
                    if len(doc_text) == 14 or len(doc_text) == 11:
                        tax_id = doc_text
                        break
            
            # Se não encontrou, tenta buscar diretamente nos filhos
            if not tax_id:
                for child in destinatario:
                    tag_local = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                    if tag_local in ['CNPJ', 'CPF'] and child.text:
                        doc_text = ''.join(filter(str.isdigit, child.text.strip()))
                        if len(doc_text) == 14 or len(doc_text) == 11:
                            tax_id = doc_text
                            break
        
        # Formata CNPJ/CPF se encontrado
        if tax_id:
            if len(tax_id) == 14:
                # Formata CNPJ: XX.XXX.XXX/XXXX-XX
                tax_id = f"{tax_id[:2]}.{tax_id[2:5]}.{tax_id[5:8]}/{tax_id[8:12]}-{tax_id[12:]}"
            elif len(tax_id) == 11:
                # Formata CPF: XXX.XXX.XXX-XX
                tax_id = f"{tax_id[:3]}.{tax_id[3:6]}.{tax_id[6:9]}-{tax_id[9:]}"
        
        # Extrai endereço do cliente
        customer_address = None
        if destinatario is not None:
            endereco = find_element_any_ns(destinatario, 'enderDest')
            if endereco is None:
                # Tenta outras variações
                endereco = find_element_any_ns(destinatario, 'ender')
            
            if endereco is not None:
                partes_endereco = []
                # Mapeamento de tags de endereço (baseado na estrutura real do XML)
                endereco_tags = {
                    'xLgr': None,  # Logradouro
                    'nro': None,   # Número
                    'xCpl': None,  # Complemento
                    'xBairro': None,  # Bairro
                    'xMun': None,  # Município
                    'UF': None,    # Estado
                    'CEP': None    # CEP (opcional, para referência)
                }
                
                for tag in endereco_tags.keys():
                    elem = find_element_any_ns(endereco, tag)
                    if elem is not None and elem.text:
                        endereco_tags[tag] = elem.text.strip()
                
                # Monta o endereço no formato padrão brasileiro
                if endereco_tags['xLgr']:
                    endereco_str = endereco_tags['xLgr']
                    
                    # Adiciona número
                    if endereco_tags['nro']:
                        endereco_str += f", {endereco_tags['nro']}"
                    
                    # Adiciona complemento se existir
                    if endereco_tags['xCpl']:
                        endereco_str += f" - {endereco_tags['xCpl']}"
                    
                    # Adiciona bairro
                    if endereco_tags['xBairro']:
                        endereco_str += f" - {endereco_tags['xBairro']}"
                    
                    # Adiciona cidade/UF
                    if endereco_tags['xMun']:
                        endereco_str += f" - {endereco_tags['xMun']}"
                        if endereco_tags['UF']:
                            endereco_str += f"/{endereco_tags['UF']}"
                    elif endereco_tags['UF']:
                        endereco_str += f" - {endereco_tags['UF']}"
                    
                    customer_address = endereco_str
        
        # Retorna os dados extraídos
        return {
            "customer_name": customer_name or "Nome não encontrado no XML",
            "customer_address": customer_address or "Endereço não encontrado no XML",
            "tax_id": tax_id or "00.000.000/0000-00"
        }
    except Exception as e:
        # Se houver erro no parsing, retorna dados genéricos
        return {
            "customer_name": "Erro ao extrair dados do XML",
            "customer_address": "Erro ao extrair endereço",
            "tax_id": "00.000.000/0000-00",
            "parse_error": str(e)
        }

def get_certificate_config():
    """Retorna a configuração do certificado (upload ou .env)"""
    # Verifica se há certificado enviado via upload
    if certificate_config["path"]:
        # Converte para caminho absoluto e verifica se existe
        cert_path = Path(certificate_config["path"]).resolve()
        if cert_path.exists():
            return {
                "path": str(cert_path),
                "password": certificate_config["password"],
                "uf": certificate_config["uf"] or UF,
                "homologacao": certificate_config["homologacao"]
            }
    
    # Tenta encontrar certificado na pasta certificates (mesmo que não tenha sido via upload)
    # Isso ajuda se o servidor foi reiniciado e perdeu a configuração em memória
    if CERT_PATH and CERT_PATH != 'certificado.pfx':
        # Tenta na raiz primeiro
        env_cert_path = Path(CERT_PATH).resolve()
        if not env_cert_path.exists():
            # Tenta na pasta certificates
            env_cert_path = CERTIFICATES_DIR / Path(CERT_PATH).name
            env_cert_path = env_cert_path.resolve()
        
        if env_cert_path.exists():
            return {
                "path": str(env_cert_path),
                "password": CERT_PASS,
                "uf": UF,
                "homologacao": HOMOLOGACAO
            }
    
    # Fallback para .env - também converte para caminho absoluto
    # Tenta primeiro na raiz do projeto
    env_cert_path = Path(CERT_PATH).resolve()
    
    # Se não encontrou na raiz, tenta na pasta certificates
    if not env_cert_path.exists() and CERT_PATH != 'certificado.pfx':
        cert_name = Path(CERT_PATH).name
        env_cert_path = CERTIFICATES_DIR / cert_name
        env_cert_path = env_cert_path.resolve()
    
    if env_cert_path.exists():
        return {
            "path": str(env_cert_path),
            "password": CERT_PASS,
            "uf": UF,
            "homologacao": HOMOLOGACAO
        }
    else:
        # Se não encontrou nem upload nem .env, retorna o caminho mesmo assim
        # para que o erro seja claro
        return {
            "path": str(env_cert_path),
            "password": CERT_PASS,
            "uf": UF,
            "homologacao": HOMOLOGACAO
        }

@app.post("/upload-certificate")
async def upload_certificate(
    file: UploadFile = File(...),
    password: str = Form(...),
    uf: str = Form(...),
    homologacao: bool = Form(False)
):
    """
    Faz upload do certificado A1 (.pfx) e configura a senha e UF.
    
    - **file**: Arquivo .pfx do certificado
    - **password**: Senha do certificado
    - **uf**: Sigla do estado (MG, SP, RJ, etc.)
    - **homologacao**: True para ambiente de homologação, False para produção
    """
    # Valida extensão do arquivo
    if not file.filename.endswith(('.pfx', '.p12')):
        raise HTTPException(
            status_code=400,
            detail="Arquivo inválido. Apenas arquivos .pfx ou .p12 são aceitos."
        )
    
    # Valida UF
    uf = uf.upper().strip()
    ufs_validas = ['AC', 'AL', 'AP', 'AM', 'BA', 'CE', 'DF', 'ES', 'GO', 'MA', 
                   'MT', 'MS', 'MG', 'PA', 'PB', 'PR', 'PE', 'PI', 'RJ', 'RN', 
                   'RS', 'RO', 'RR', 'SC', 'SP', 'SE', 'TO']
    if uf not in ufs_validas:
        raise HTTPException(
            status_code=400,
            detail=f"UF inválida. Use uma das siglas válidas: {', '.join(ufs_validas)}"
        )
    
    # Salva o arquivo (usa caminho absoluto)
    file_path = CERTIFICATES_DIR / file.filename
    file_path = file_path.resolve()  # Converte para caminho absoluto
    
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao salvar certificado: {str(e)}"
        )
    
    # Testa se o certificado pode ser aberto com a senha fornecida
    try:
        con = ComunicacaoSefaz(uf, str(file_path), password, homologacao=homologacao)
        # Se chegou aqui, o certificado foi aberto com sucesso
    except Exception as e:
        # Remove o arquivo se a senha estiver incorreta
        if os.path.exists(file_path):
            os.remove(file_path)
        
        error_msg = str(e)
        if "senha" in error_msg.lower() or "password" in error_msg.lower():
            raise HTTPException(
                status_code=400,
                detail="Senha incorreta ou certificado inválido. Verifique a senha e tente novamente."
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Erro ao validar certificado: {error_msg}"
            )
    
    # Salva as configurações (usa caminho absoluto)
    certificate_config["path"] = str(file_path.resolve())
    certificate_config["password"] = password
    certificate_config["uf"] = uf
    certificate_config["homologacao"] = homologacao
    
    # Verifica se o arquivo realmente foi salvo
    if not file_path.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Erro: Certificado não foi salvo corretamente em {file_path}"
        )
    
    return {
        "message": "Certificado carregado e validado com sucesso!",
        "filename": file.filename,
        "uf": uf,
        "homologacao": homologacao,
        "path": str(file_path.resolve())
    }


class XMLNFeRequest(BaseModel):
    xml: str

def extract_nfe_complete_data(xml_content: str) -> dict:
    """
    Extrai dados completos da NF-e incluindo valor total.
    Retorna: nome, endereço, CNPJ/CPF e valor total.
    """
    try:
        # Faz parsing do XML
        if isinstance(xml_content, str):
            # Remove BOM se presente
            if xml_content.startswith('\ufeff'):
                xml_content = xml_content[1:]
            
            # Valida se é XML
            if not xml_content.strip().startswith('<?xml') and not xml_content.strip().startswith('<'):
                raise ValueError("Conteúdo fornecido não é um XML válido")
            
            # Tenta fazer parsing
            try:
                root = etree.fromstring(xml_content.encode('utf-8'))
            except Exception as e:
                # Se falhar, tenta sem encoding explícito
                try:
                    root = etree.fromstring(xml_content)
                except:
                    raise ValueError(f"Erro ao fazer parse do XML: {str(e)}")
        else:
            root = xml_content
        
        # Namespace padrão da NF-e - tenta descobrir o namespace do XML
        ns_uri = None
        if '}' in root.tag:
            ns_uri = root.tag.split('}')[0][1:]
        else:
            # Tenta encontrar o namespace em qualquer elemento
            for elem in root.iter():
                if '}' in elem.tag:
                    ns_uri = elem.tag.split('}')[0][1:]
                    break
        
        # Se não encontrou namespace, usa o padrão
        if not ns_uri:
            ns_uri = 'http://www.portalfiscal.inf.br/nfe'
        
        ns = {'ns': ns_uri}
        
        # Extrai nome do destinatário (xNome) usando lxml com namespace da NF-e
        customer_name = None
        try:
            # Tenta com namespace primeiro
            customer_name_nodes = root.xpath('//ns:dest/ns:xNome/text()', namespaces=ns)
            if not customer_name_nodes:
                # Tenta sem namespace
                customer_name_nodes = root.xpath('//dest/xNome/text()')
            if not customer_name_nodes:
                # Tenta caminho alternativo
                customer_name_nodes = root.xpath('//*[local-name()="dest"]/*[local-name()="xNome"]/text()')
            
            if customer_name_nodes:
                customer_name = customer_name_nodes[0].strip()
        except Exception as e:
            # Se falhar, tenta método alternativo
            try:
                dest_elem = root.find('.//{http://www.portalfiscal.inf.br/nfe}dest') or root.find('.//dest')
                if dest_elem is not None:
                    xnome_elem = dest_elem.find('.//{http://www.portalfiscal.inf.br/nfe}xNome') or dest_elem.find('.//xNome')
                    if xnome_elem is not None and xnome_elem.text:
                        customer_name = xnome_elem.text.strip()
            except:
                pass
        
        # Extrai CPF/CNPJ
        tax_id = None
        try:
            tax_id_nodes = root.xpath('//ns:dest/ns:CPF/text() | //ns:dest/ns:CNPJ/text()', namespaces=ns)
            if not tax_id_nodes:
                tax_id_nodes = root.xpath('//dest/CPF/text() | //dest/CNPJ/text()')
            tax_id_raw = tax_id_nodes[0].strip() if tax_id_nodes else None
            
            # Formata CPF/CNPJ
            if tax_id_raw:
                doc_text = ''.join(filter(str.isdigit, tax_id_raw))
                if len(doc_text) == 14:
                    tax_id = f"{doc_text[:2]}.{doc_text[2:5]}.{doc_text[5:8]}/{doc_text[8:12]}-{doc_text[12:]}"
                elif len(doc_text) == 11:
                    tax_id = f"{doc_text[:3]}.{doc_text[3:6]}.{doc_text[6:9]}-{doc_text[9:]}"
                else:
                    tax_id = tax_id_raw
        except:
            pass
        
        # Extrai endereço (xLgr, nro, xBairro) usando lxml com namespace da NF-e
        endereco_data = {
            'logradouro': None,
            'numero': None,
            'bairro': None,
            'completo': None
        }
        try:
            # Busca o elemento enderDest primeiro
            ender_dest = None
            try:
                ender_dest = root.find('.//{http://www.portalfiscal.inf.br/nfe}dest/{http://www.portalfiscal.inf.br/nfe}enderDest', namespaces=ns)
                if ender_dest is None:
                    ender_dest = root.find('.//dest/enderDest')
                if ender_dest is None:
                    # Tenta com xpath
                    ender_nodes = root.xpath('//ns:dest/ns:enderDest', namespaces=ns)
                    if ender_nodes:
                        ender_dest = ender_nodes[0]
            except:
                pass
            
            if ender_dest is not None:
                # Extrai xLgr (logradouro)
                xlgr_elem = ender_dest.find('.//{http://www.portalfiscal.inf.br/nfe}xLgr') or ender_dest.find('.//xLgr')
                if xlgr_elem is not None and xlgr_elem.text:
                    endereco_data['logradouro'] = xlgr_elem.text.strip()
                
                # Extrai nro (número)
                nro_elem = ender_dest.find('.//{http://www.portalfiscal.inf.br/nfe}nro') or ender_dest.find('.//nro')
                if nro_elem is not None and nro_elem.text:
                    endereco_data['numero'] = nro_elem.text.strip()
                
                # Extrai xBairro (bairro)
                xbairro_elem = ender_dest.find('.//{http://www.portalfiscal.inf.br/nfe}xBairro') or ender_dest.find('.//xBairro')
                if xbairro_elem is not None and xbairro_elem.text:
                    endereco_data['bairro'] = xbairro_elem.text.strip()
            
            # Se não encontrou com find, tenta com xpath
            if not endereco_data['logradouro']:
                xlgr_nodes = root.xpath('//ns:dest/ns:enderDest/ns:xLgr/text()', namespaces=ns)
                if not xlgr_nodes:
                    xlgr_nodes = root.xpath('//dest/enderDest/xLgr/text()')
                if xlgr_nodes:
                    endereco_data['logradouro'] = xlgr_nodes[0].strip()
            
            if not endereco_data['numero']:
                nro_nodes = root.xpath('//ns:dest/ns:enderDest/ns:nro/text()', namespaces=ns)
                if not nro_nodes:
                    nro_nodes = root.xpath('//dest/enderDest/nro/text()')
                if nro_nodes:
                    endereco_data['numero'] = nro_nodes[0].strip()
            
            if not endereco_data['bairro']:
                xbairro_nodes = root.xpath('//ns:dest/ns:enderDest/ns:xBairro/text()', namespaces=ns)
                if not xbairro_nodes:
                    xbairro_nodes = root.xpath('//dest/enderDest/xBairro/text()')
                if xbairro_nodes:
                    endereco_data['bairro'] = xbairro_nodes[0].strip()
            
            # Monta endereço completo
            partes = []
            if endereco_data['logradouro']:
                partes.append(endereco_data['logradouro'])
            if endereco_data['numero']:
                partes.append(f", {endereco_data['numero']}")
            if endereco_data['bairro']:
                partes.append(f" - {endereco_data['bairro']}")
            
            endereco_data['completo'] = ''.join(partes) if partes else None
        except Exception as e:
            # Log do erro para debug (pode ser removido em produção)
            pass
        
        # Extrai valor total da nota (vNF) usando lxml com namespace da NF-e
        valor_total = None
        try:
            # Tenta caminho completo primeiro
            vnf_nodes = root.xpath('//ns:total/ns:ICMSTot/ns:vNF/text()', namespaces=ns)
            if not vnf_nodes:
                vnf_nodes = root.xpath('//total/ICMSTot/vNF/text()')
            if not vnf_nodes:
                # Tenta outros caminhos possíveis
                vnf_nodes = root.xpath('//ns:vNF/text()', namespaces=ns)
            if not vnf_nodes:
                vnf_nodes = root.xpath('//vNF/text()')
            
            if vnf_nodes:
                valor_total = float(vnf_nodes[0].strip())
            else:
                # Tenta método alternativo com find
                try:
                    total_elem = root.find('.//{http://www.portalfiscal.inf.br/nfe}total/{http://www.portalfiscal.inf.br/nfe}ICMSTot/{http://www.portalfiscal.inf.br/nfe}vNF')
                    if total_elem is None:
                        total_elem = root.find('.//total/ICMSTot/vNF')
                    if total_elem is not None and total_elem.text:
                        valor_total = float(total_elem.text.strip())
                except:
                    pass
        except Exception as e:
            pass
        
        # Valida se encontrou pelo menos o nome do destinatário
        # Se não encontrou, retorna erro claro
        if not customer_name:
            raise ValueError("XML não contém a tag de destinatário (xNome). Verifique se é um XML de NF-e válido. O XML deve conter a estrutura <dest><xNome>...</xNome></dest>.")
        
        # Retorna os dados extraídos do XML (sem dados mockados)
        return {
            "nome_destinatario": customer_name,
            "endereco": endereco_data,
            "cnpj_cpf": tax_id,
            "valor_total": valor_total
        }
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Erro ao processar XML: {str(e)}")

@app.post("/nfe/parse-xml")
async def parse_nfe_xml(request: XMLNFeRequest):
    """
    Faz parse local do XML da NF-e e extrai dados do destinatário e valor total.
    
    Este endpoint não consulta a SEFAZ, apenas faz parse do XML fornecido.
    Útil para processar XMLs já armazenados localmente.
    
    - **xml**: String contendo o XML completo da NF-e
    
    Retorna formato compatível com NestJS:
    - name: Nome do destinatário (xNome)
    - address: Endereço completo formatado
    - taxId: CNPJ ou CPF formatado do destinatário
    - valorTotal: Valor total da nota (vNF)
    """
    try:
        xml_content = request.xml.strip()
        
        # Valida se o XML não está vazio
        if not xml_content:
            raise HTTPException(
                status_code=400,
                detail="XML não pode estar vazio"
            )
        
        # Valida se parece com XML
        if not xml_content.startswith('<?xml') and not xml_content.startswith('<'):
            raise HTTPException(
                status_code=400,
                detail="Conteúdo fornecido não é um XML válido"
            )
        
        # Extrai os dados do XML usando lxml com namespaces da NF-e
        dados = extract_nfe_complete_data(xml_content)
        
        # Retorna JSON plano com os dados extraídos
        # Formato compatível com NestJS: name, address, taxId
        nome_extraido = dados.get("nome_destinatario")
        endereco_extraido = dados.get("endereco", {}).get("completo") or ""
        cnpj_extraido = dados.get("cnpj_cpf")
        
        return {
            "name": nome_extraido,
            "address": endereco_extraido,
            "taxId": cnpj_extraido
        }
        
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao processar XML: {str(e)}"
        )

@app.post("/nfe/extract-from-xml")
async def extract_nfe_from_xml(request: XMLNFeRequest):
    """
    Extrai dados da NF-e a partir do XML completo fornecido diretamente.
    
    Útil quando:
    - Você já tem o XML da NF-e
    - O certificado não tem permissão para consultar via distribuição DF-e
    - Você é pessoa física mas o certificado é da empresa
    
    - **xml**: String contendo o XML completo da NF-e
    """
    try:
        xml_content = request.xml.strip()
        
        # Valida se parece com XML
        if not xml_content.startswith('<?xml') and not xml_content.startswith('<'):
            raise HTTPException(
                status_code=400,
                detail="Conteúdo fornecido não parece ser um XML válido"
            )
        
        # Extrai os dados do XML
        dados_nfe = extract_nfe_data_from_xml(xml_content)
        
        # Tenta extrair a chave de acesso do XML
        nfe_key = None
        try:
            root = etree.fromstring(xml_content.encode('utf-8'))
            # Busca a chave de acesso
            chave_elem = root.find('.//{http://www.portalfiscal.inf.br/nfe}chNFe') or root.find('.//chNFe')
            if chave_elem is None:
                # Tenta buscar em infNFe
                inf_nfe = root.find('.//{http://www.portalfiscal.inf.br/nfe}infNFe') or root.find('.//infNFe')
                if inf_nfe is not None:
                    chave_elem = inf_nfe.get('Id')
                    if chave_elem and chave_elem.startswith('NFe'):
                        nfe_key = chave_elem.replace('NFe', '')
            else:
                nfe_key = chave_elem.text.strip() if chave_elem.text else None
            
            # Se não encontrou, tenta buscar no Id do infNFe
            if not nfe_key:
                for elem in root.iter():
                    if 'Id' in elem.attrib and elem.attrib['Id'].startswith('NFe'):
                        nfe_key = elem.attrib['Id'].replace('NFe', '')
                        break
        except:
            pass
        
        return {
            "success": True,
            "nfe_key": nfe_key or "Não encontrada no XML",
            "xml_available": True,
            **dados_nfe
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao processar XML: {str(e)}"
        )

@app.post("/nfe/extract-from-xml-file")
async def extract_nfe_from_xml_file(file: UploadFile = File(...)):
    """
    Extrai dados da NF-e a partir de um arquivo XML enviado.
    
    Útil quando:
    - Você já tem o arquivo XML da NF-e
    - O certificado não tem permissão para consultar via distribuição DF-e
    - Você é pessoa física mas o certificado é da empresa
    
    - **file**: Arquivo XML da NF-e
    """
    try:
        # Valida se é um arquivo XML
        if not file.filename.endswith('.xml') and file.content_type not in ['application/xml', 'text/xml']:
            raise HTTPException(
                status_code=400,
                detail="Arquivo inválido. Apenas arquivos XML são aceitos."
            )
        
        # Lê o conteúdo do arquivo
        xml_content = await file.read()
        xml_content = xml_content.decode('utf-8').strip()
        
        # Extrai os dados do XML
        dados_nfe = extract_nfe_data_from_xml(xml_content)
        
        # Tenta extrair a chave de acesso do XML
        nfe_key = None
        try:
            root = etree.fromstring(xml_content.encode('utf-8'))
            # Busca a chave de acesso
            chave_elem = root.find('.//{http://www.portalfiscal.inf.br/nfe}chNFe') or root.find('.//chNFe')
            if chave_elem is None:
                # Tenta buscar em infNFe
                inf_nfe = root.find('.//{http://www.portalfiscal.inf.br/nfe}infNFe') or root.find('.//infNFe')
                if inf_nfe is not None:
                    chave_elem = inf_nfe.get('Id')
                    if chave_elem and chave_elem.startswith('NFe'):
                        nfe_key = chave_elem.replace('NFe', '')
            else:
                nfe_key = chave_elem.text.strip() if chave_elem.text else None
            
            # Se não encontrou, tenta buscar no Id do infNFe
            if not nfe_key:
                for elem in root.iter():
                    if 'Id' in elem.attrib and elem.attrib['Id'].startswith('NFe'):
                        nfe_key = elem.attrib['Id'].replace('NFe', '')
                        break
        except:
            pass
        
        return {
            "success": True,
            "nfe_key": nfe_key or "Não encontrada no XML",
            "xml_available": True,
            "filename": file.filename,
            **dados_nfe
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao processar XML: {str(e)}"
        )

@app.get("/certificate/status")
async def get_certificate_status():
    """Retorna o status atual do certificado configurado"""
    config = get_certificate_config()
    
    cert_path = Path(config["path"])
    cert_exists = cert_path.exists()
    
    # Informações de debug
    debug_info = {
        "configured": cert_exists,
        "path": config["path"],
        "path_absolute": str(cert_path.resolve()) if cert_path else None,
        "path_exists": cert_exists,
        "uf": config["uf"],
        "homologacao": config["homologacao"],
        "source": "upload" if certificate_config["path"] else ".env",
        "upload_config": {
            "path": certificate_config["path"],
            "path_exists": os.path.exists(certificate_config["path"]) if certificate_config["path"] else False
        } if certificate_config["path"] else None,
        "certificates_dir": str(CERTIFICATES_DIR.resolve()),
        "certificates_dir_exists": CERTIFICATES_DIR.exists()
    }
    
    return debug_info


@app.delete("/certificate")
async def delete_certificate():
    """Remove o certificado enviado (volta para o .env)"""
    if certificate_config["path"] and os.path.exists(certificate_config["path"]):
        try:
            os.remove(certificate_config["path"])
        except Exception as e:
            pass  # Ignora erro ao remover
    
    certificate_config["path"] = None
    certificate_config["password"] = None
    certificate_config["uf"] = None
    certificate_config["homologacao"] = False
    
    return {"message": "Certificado removido. Sistema voltará a usar configurações do .env"}


@app.get("/nfe/{nfe_key}")
async def get_nfe_data(nfe_key: str):
    """
    Consulta NF-e pela chave de acesso via SEFAZ.
    
    IMPORTANTE: Este endpoint requer integração real com SEFAZ e certificado válido.
    Se não houver integração configurada ou se a consulta falhar, retorna erro 404.
    
    Para processar XMLs já armazenados, use POST /nfe/parse-xml
    """
    # Remove espaços e caracteres não numéricos
    nfe_key_clean = ''.join(filter(str.isdigit, nfe_key))
    
    # Valida comprimento: NF-e tem 44 dígitos, NFS-e pode ter formatos diferentes (geralmente 50-56)
    if len(nfe_key_clean) == 44:
        # NF-e padrão (modelo 55)
        tipo_nota = 'nfe'
        modelo = 'nfe'
    elif 50 <= len(nfe_key_clean) <= 56:
        # Provavelmente NFS-e (Nota Fiscal de Serviços Eletrônica)
        # NFS-e é emitida pelas prefeituras e tem formato diferente
        tipo_nota = 'nfse'
        modelo = 'nfse'
        # Para NFS-e, não podemos usar o mesmo método de consulta da SEFAZ
        # Retorna erro informativo
        raise HTTPException(
            status_code=400,
            detail=f"NFS-e (Nota Fiscal de Serviços) detectada ({len(nfe_key_clean)} dígitos). "
                   f"NFS-e é emitida pelas prefeituras e requer integração específica com o sistema municipal. "
                   f"Este serviço atualmente suporta apenas NF-e (44 dígitos) da SEFAZ. "
                   f"Para processar NFS-e, use o endpoint POST /nfe/parse-xml com o XML completo."
        )
    else:
        raise HTTPException(
            status_code=400, 
            detail=f"Chave inválida. NF-e deve ter 44 dígitos, NFS-e geralmente tem 50-56 dígitos. "
                   f"Chave recebida tem {len(nfe_key_clean)} dígitos."
        )
    
    # Usa a chave limpa
    nfe_key = nfe_key_clean

    # Detecta a UF da chave de acesso (primeiros 2 dígitos)
    uf_da_chave = detect_uf_from_key(nfe_key)
    
    # Obtém configuração do certificado (upload ou .env)
    cert_config = get_certificate_config()
    
    # Verifica se há certificado configurado
    if not cert_config["path"] or not Path(cert_config["path"]).exists():
        raise HTTPException(
            status_code=404,
            detail="Certificado não configurado ou não encontrado. "
                   "Configure um certificado via POST /upload-certificate ou arquivo .env. "
                   "Alternativamente, use POST /nfe/parse-xml com o XML completo da nota."
        )
    
    # Se a UF da chave for diferente da configurada, usa a UF da chave
    # Isso permite consultar notas de qualquer estado
    if uf_da_chave and uf_da_chave != cert_config["uf"]:
        # Avisa que está usando a UF da chave, não a do certificado
        # Nota: O certificado precisa ter permissão para consultar notas de outros estados
        cert_config["uf"] = uf_da_chave

    # Verifica se o certificado existe
    if not os.path.exists(cert_config["path"]):
        raise HTTPException(
            status_code=500,
            detail=f"Certificado não encontrado em: {cert_config['path']}. Faça upload de um certificado ou configure o .env"
        )

    try:
        # Inicializa a comunicação com a SEFAZ
        # Isso também testa se o certificado pode ser aberto com a senha fornecida
        con = ComunicacaoSefaz(
            cert_config["uf"], 
            cert_config["path"], 
            cert_config["password"], 
            homologacao=cert_config["homologacao"]
        )
        
        # Método correto: consulta_nota (não consultar_nota)
        # Este método consulta a situação da NF-e pela chave de acesso
        # Modelo: "nfe" para NF-e (modelo 55) ou "nfce" para NFC-e (modelo 65)
        try:
            resposta = con.consulta_nota(chave=nfe_key, modelo='nfe')
            
            # Verifica se a resposta é válida
            if resposta is None:
                raise Exception("Resposta da consulta retornou None")
            
            # Armazena o XML da resposta para possível extração de dados
            xml_resposta_consulta = None
            
            # Se a resposta é um objeto Response do requests, precisa processar o XML
            if isinstance(resposta, requests.Response):
                # Extrai o conteúdo XML da resposta
                xml_content = resposta.text
                xml_resposta_consulta = xml_content  # Guarda para possível uso posterior
                if not xml_content:
                    raise Exception("Resposta HTTP não contém conteúdo XML")
                
                # Faz o parsing do XML
                try:
                    root = etree.fromstring(xml_content.encode('utf-8'))
                except Exception as parse_error:
                    # Tenta sem encoding se falhar
                    root = etree.fromstring(xml_content)
                
                # Namespaces comuns da SEFAZ
                namespaces = {
                    'nfe': 'http://www.portalfiscal.inf.br/nfe',
                    'soap': 'http://schemas.xmlsoap.org/soap/envelope/',
                    'soap12': 'http://www.w3.org/2003/05/soap-envelope'
                }
                
                # Tenta encontrar cStat e xMotivo no XML
                status_code = None
                motivo = None
                
                # Busca cStat em diferentes namespaces e locais
                search_paths = [
                    './/cStat',
                    './/cstat',
                    './/{http://www.portalfiscal.inf.br/nfe}cStat',
                    './/{http://www.portalfiscal.inf.br/nfe}cstat',
                    './/soap:Body//cStat',
                    './/soap12:Body//cStat',
                    './/retConsSitNFe//cStat',
                    './/retConsSitNFe//cstat',
                ]
                
                for path in search_paths:
                    try:
                        elem = root.find(path, namespaces=namespaces) if ':' in path or '{' in path else root.find(path)
                        if elem is not None and elem.text:
                            status_code = elem.text.strip()
                            break
                    except:
                        continue
                
                # Busca xMotivo em diferentes namespaces e locais
                motivo_paths = [
                    './/xMotivo',
                    './/xmotivo',
                    './/xMsg',
                    './/{http://www.portalfiscal.inf.br/nfe}xMotivo',
                    './/{http://www.portalfiscal.inf.br/nfe}xmotivo',
                    './/soap:Body//xMotivo',
                    './/soap12:Body//xMotivo',
                    './/retConsSitNFe//xMotivo',
                    './/retConsSitNFe//xmotivo',
                ]
                
                for path in motivo_paths:
                    try:
                        elem = root.find(path, namespaces=namespaces) if ':' in path or '{' in path else root.find(path)
                        if elem is not None and elem.text:
                            motivo = elem.text.strip()
                            break
                    except:
                        continue
                
                # Se ainda não encontrou, tenta buscar todos os elementos e procurar manualmente
                if status_code is None or motivo is None:
                    all_elements = root.iter()
                    for elem in all_elements:
                        tag_name = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                        if tag_name.lower() in ['cstat', 'c_stat'] and elem.text:
                            status_code = elem.text.strip()
                        if tag_name.lower() in ['xmotivo', 'x_motivo', 'xmsg'] and elem.text:
                            motivo = elem.text.strip()
                        if status_code and motivo:
                            break
                
            else:
                # Se não é Response, tenta acessar como objeto normal
                status_code = getattr(resposta, 'cStat', None)
                motivo = getattr(resposta, 'xMotivo', None)
            
            # Se ainda não encontrou, tenta outros atributos
            if status_code is None:
                status_code = getattr(resposta, 'status', None) or getattr(resposta, 'codigo', None)
            if motivo is None:
                motivo = getattr(resposta, 'xMotivo', None) or getattr(resposta, 'motivo', None) or 'Consulta realizada'
            
            # Se ainda não encontrou, tenta extrair do XML diretamente como string
            if status_code is None:
                # Última tentativa: busca no texto do XML
                if isinstance(resposta, requests.Response):
                    xml_text = resposta.text
                    import re
                    # Busca padrões como <cStat>100</cStat> ou cStat="100"
                    cstat_match = re.search(r'<cStat[^>]*>(\d+)</cStat>', xml_text, re.IGNORECASE)
                    if cstat_match:
                        status_code = cstat_match.group(1)
                    
                    xmotivo_match = re.search(r'<xMotivo[^>]*>(.*?)</xMotivo>', xml_text, re.IGNORECASE | re.DOTALL)
                    if xmotivo_match and not motivo:
                        motivo = xmotivo_match.group(1).strip()
                
                # Se ainda não encontrou, retorna erro com parte do XML para debug
                if status_code is None:
                    xml_preview = resposta.text[:500] if isinstance(resposta, requests.Response) else str(resposta)[:500]
                    raise Exception(f"Resposta não contém status. XML preview: {xml_preview}")
            
            # Se a nota foi encontrada (status 100 = autorizada)
            if str(status_code) == '100':
                # Tenta obter o XML via consulta_distribuicao para extrair dados completos
                xml_nfe = None
                xml_error_msg = None
                
                try:
                    # Extrai CNPJ da chave de acesso (posições 6 a 19)
                    cnpj_chave = nfe_key[6:20]
                    
                    resposta_dist = con.consulta_distribuicao(
                        cnpj=cnpj_chave,
                        chave=nfe_key
                    )
                    
                    # Processa o XML se disponível
                    if isinstance(resposta_dist, requests.Response):
                        xml_content = resposta_dist.text
                        if xml_content:
                            try:
                                root = etree.fromstring(xml_content.encode('utf-8'))
                                
                                # Namespaces para busca
                                ns = {'nfe': 'http://www.portalfiscal.inf.br/nfe'}
                                
                                # Verifica o status da distribuição DF-e primeiro
                                dist_status = None
                                dist_motivo = None
                                
                                # Busca o status da distribuição
                                status_paths = [
                                    './/cStat',
                                    './/{http://www.portalfiscal.inf.br/nfe}cStat',
                                    './/retDistDFeInt//cStat',
                                    './/loteDistDFeInt//cStat'
                                ]
                                
                                for path in status_paths:
                                    try:
                                        elem = root.find(path, namespaces=ns) if ':' in path or '{' in path else root.find(path)
                                        if elem is not None and elem.text:
                                            dist_status = elem.text.strip()
                                            break
                                    except:
                                        continue
                                
                                # Busca o motivo
                                motivo_paths = [
                                    './/xMotivo',
                                    './/{http://www.portalfiscal.inf.br/nfe}xMotivo',
                                    './/retDistDFeInt//xMotivo',
                                    './/loteDistDFeInt//xMotivo'
                                ]
                                
                                for path in motivo_paths:
                                    try:
                                        elem = root.find(path, namespaces=ns) if ':' in path or '{' in path else root.find(path)
                                        if elem is not None and elem.text:
                                            dist_motivo = elem.text.strip()
                                            break
                                    except:
                                        continue
                                
                                # Se a distribuição retornou erro, tenta fazer manifestação do destinatário
                                if dist_status and dist_status != '138':  # 138 = Documento localizado
                                    # Se for erro 656 (Consumo Indevido), tenta fazer manifestação
                                    if dist_status == '656':
                                        try:
                                            # Tenta fazer manifestação do destinatário (Ciência da Operação)
                                            # Isso pode liberar o XML para consulta
                                            # Extrai CNPJ/CPF da chave (posições 6 a 19 para CNPJ, ou pode ser CPF)
                                            cnpj_cpf_chave = nfe_key[6:20]
                                            
                                            # Tenta fazer a manifestação usando o método evento do pynfe
                                            # Tipo de evento: 210200 = Ciência da Operação
                                            # Nota: Isso pode não funcionar se o certificado não for do destinatário
                                            # Mas vamos tentar mesmo assim
                                            manifestacao_feita = False
                                            
                                            # Verifica se o método evento existe
                                            if hasattr(con, 'evento'):
                                                try:
                                                    # Tenta fazer a manifestação
                                                    # Parâmetros podem variar conforme a biblioteca
                                                    resultado_manifestacao = con.evento(
                                                        cnpj=cnpj_cpf_chave,
                                                        chave=nfe_key,
                                                        tipo_evento='210200'  # Ciência da Operação
                                                    )
                                                    
                                                    # Se a manifestação foi bem-sucedida, tenta consultar novamente
                                                    if resultado_manifestacao:
                                                        manifestacao_feita = True
                                                        # Aguarda um pouco antes de tentar novamente
                                                        import time
                                                        time.sleep(2)
                                                        
                                                        # Tenta consultar a distribuição novamente
                                                        resposta_dist_retry = con.consulta_distribuicao(
                                                            cnpj=cnpj_cpf_chave,
                                                            chave=nfe_key
                                                        )
                                                        
                                                        if isinstance(resposta_dist_retry, requests.Response):
                                                            xml_content_retry = resposta_dist_retry.text
                                                            if xml_content_retry:
                                                                root_retry = etree.fromstring(xml_content_retry.encode('utf-8'))
                                                                # Verifica se agora tem docZip
                                                                doc_zip_retry = root_retry.find('.//docZip') or root_retry.find('.//{http://www.portalfiscal.inf.br/nfe}docZip')
                                                                if doc_zip_retry is not None and doc_zip_retry.text:
                                                                    # Se encontrou docZip, processa normalmente
                                                                    xml_content = xml_content_retry
                                                                    root = root_retry
                                                                    dist_status = '138'  # Marca como sucesso para processar
                                                                    dist_motivo = "Manifestação realizada com sucesso"
                                                except Exception as manifest_error:
                                                    # Se a manifestação falhar, continua com o erro original
                                                    pass
                                            
                                            if not manifestacao_feita:
                                                xml_error_msg = f"Distribuição DF-e retornou status {dist_status}: {dist_motivo or 'Erro desconhecido'}. O XML completo não está disponível via distribuição DF-e. Tentativa de manifestação automática não foi possível."
                                                xml_nfe = None
                                            else:
                                                # Se a manifestação foi feita mas ainda não tem XML, continua processando
                                                xml_error_msg = None
                                        except Exception as manifest_error:
                                            xml_error_msg = f"Distribuição DF-e retornou status {dist_status}: {dist_motivo or 'Erro desconhecido'}. Tentativa de manifestação falhou: {str(manifest_error)}"
                                            xml_nfe = None
                                    else:
                                        xml_error_msg = f"Distribuição DF-e retornou status {dist_status}: {dist_motivo or 'Erro desconhecido'}. O XML completo não está disponível via distribuição DF-e."
                                        xml_nfe = None
                                else:
                                    # PRIORIDADE 1: Procura pela tag docZip (XML completo compactado)
                                    # A distribuição DF-e retorna o XML completo dentro de docZip em base64 + gzip
                                    doc_zip_found = False
                                
                                # Tenta usar xpath primeiro (mais eficiente)
                                try:
                                    # Namespace para distribuição DF-e
                                    ns_dist = {'ns': 'http://www.portalfiscal.inf.br/nfe'}
                                    doc_zip_nodes = root.xpath('//ns:docZip/text()', namespaces=ns_dist)
                                    
                                    if not doc_zip_nodes:
                                        # Tenta sem namespace
                                        doc_zip_nodes = root.xpath('//docZip/text()')
                                    
                                    if doc_zip_nodes and doc_zip_nodes[0]:
                                        doc_zip_text = doc_zip_nodes[0].strip()
                                        try:
                                            # XML está em base64 e compactado com gzip
                                            xml_compressed = base64.b64decode(doc_zip_text)
                                            xml_nfe = gzip.decompress(xml_compressed).decode('utf-8')
                                            
                                            # Garante que o XML tem a declaração XML
                                            if not xml_nfe.strip().startswith('<?xml'):
                                                xml_nfe = '<?xml version="1.0" encoding="UTF-8"?>' + xml_nfe
                                            
                                            doc_zip_found = True
                                        except Exception as decompress_error:
                                            # Tenta sem descompactar (pode já estar descompactado)
                                            try:
                                                xml_nfe = base64.b64decode(doc_zip_text).decode('utf-8')
                                                if not xml_nfe.strip().startswith('<?xml'):
                                                    xml_nfe = '<?xml version="1.0" encoding="UTF-8"?>' + xml_nfe
                                                doc_zip_found = True
                                            except:
                                                pass
                                except:
                                    pass
                                
                                # Se xpath não funcionou, tenta com find
                                if not doc_zip_found:
                                    # Tenta diferentes caminhos para encontrar docZip
                                    doc_zip_paths = [
                                        './/docZip',
                                        './/{http://www.portalfiscal.inf.br/nfe}docZip',
                                        './/loteDistDFeInt//docZip',
                                        './/retDistDFeInt//docZip',
                                        './/retDistDFeInt//loteDistDFeInt//docZip'
                                    ]
                                    
                                    for doc_zip_path in doc_zip_paths:
                                        try:
                                            # Tenta encontrar com namespace
                                            doc_zip = root.find(doc_zip_path, namespaces=ns)
                                            if doc_zip is None:
                                                # Tenta sem namespace explícito
                                                doc_zip = root.find(doc_zip_path.replace('{http://www.portalfiscal.inf.br/nfe}', ''))
                                            
                                            if doc_zip is not None and doc_zip.text:
                                                try:
                                                    # XML está em base64 e compactado com gzip
                                                    xml_compressed = base64.b64decode(doc_zip.text.strip())
                                                    xml_nfe = gzip.decompress(xml_compressed).decode('utf-8')
                                                    
                                                    # Garante que o XML tem a declaração XML
                                                    if not xml_nfe.strip().startswith('<?xml'):
                                                        xml_nfe = '<?xml version="1.0" encoding="UTF-8"?>' + xml_nfe
                                                    
                                                    doc_zip_found = True
                                                    break  # Se conseguiu, para de procurar
                                                except Exception as decompress_error:
                                                    # Tenta sem descompactar (pode já estar descompactado)
                                                    try:
                                                        xml_nfe = base64.b64decode(doc_zip.text.strip()).decode('utf-8')
                                                        if not xml_nfe.strip().startswith('<?xml'):
                                                            xml_nfe = '<?xml version="1.0" encoding="UTF-8"?>' + xml_nfe
                                                        doc_zip_found = True
                                                        break
                                                    except:
                                                        continue
                                        except:
                                            continue
                                    
                                    # Última tentativa: busca por iteração
                                    if not doc_zip_found:
                                        for elem in root.iter():
                                            tag_local = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                                            if tag_local == 'docZip' and elem.text and len(elem.text.strip()) > 100:
                                                try:
                                                    xml_compressed = base64.b64decode(elem.text.strip())
                                                    xml_nfe = gzip.decompress(xml_compressed).decode('utf-8')
                                                    if not xml_nfe.strip().startswith('<?xml'):
                                                        xml_nfe = '<?xml version="1.0" encoding="UTF-8"?>' + xml_nfe
                                                    doc_zip_found = True
                                                    break
                                                except:
                                                    try:
                                                        xml_nfe = base64.b64decode(elem.text.strip()).decode('utf-8')
                                                        if not xml_nfe.strip().startswith('<?xml'):
                                                            xml_nfe = '<?xml version="1.0" encoding="UTF-8"?>' + xml_nfe
                                                        doc_zip_found = True
                                                        break
                                                    except:
                                                        continue
                                
                                # PRIORIDADE 2: Se não encontrou docZip, tenta buscar XML direto (caso já esteja descompactado)
                                if not doc_zip_found:
                                    # Procura pelo XML da NF-e em diferentes formatos
                                    nfe_elements = (
                                        root.findall('.//NFe', namespaces=ns) or
                                        root.findall('.//{http://www.portalfiscal.inf.br/nfe}NFe') or
                                        root.findall('.//NFe') or
                                        [elem for elem in root.iter() if 'NFe' in elem.tag]
                                    )
                                    
                                    if nfe_elements:
                                        # Pega o elemento NFe completo
                                        nfe_root = nfe_elements[0]
                                        # Converte o elemento NFe completo para string XML
                                        xml_nfe = etree.tostring(nfe_root, encoding='unicode', pretty_print=False)
                                        
                                        # Verifica se o XML contém a estrutura esperada (infNFe e dest)
                                        if '<infNFe' not in xml_nfe or '<dest' not in xml_nfe:
                                            # Se não tem infNFe ou dest, tenta pegar o nfeProc completo
                                            nfe_proc = root.find('.//nfeProc', namespaces=ns) or root.find('.//{http://www.portalfiscal.inf.br/nfe}nfeProc')
                                            if nfe_proc is not None:
                                                xml_nfe = etree.tostring(nfe_proc, encoding='unicode', pretty_print=False)
                                            else:
                                                # Tenta pegar o XML completo da resposta
                                                xml_nfe = xml_content
                                    
                                    # Se ainda não encontrou, procura por qualquer elemento com XML em base64
                                    if not xml_nfe:
                                        for elem in root.iter():
                                            tag_local = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                                            if tag_local == 'docZip' and elem.text and len(elem.text) > 100:
                                                try:
                                                    decoded = base64.b64decode(elem.text.strip())
                                                    try:
                                                        xml_nfe = gzip.decompress(decoded).decode('utf-8')
                                                    except:
                                                        xml_nfe = decoded.decode('utf-8')
                                                    if xml_nfe and '<NFe' in xml_nfe:
                                                        if not xml_nfe.strip().startswith('<?xml'):
                                                            xml_nfe = '<?xml version="1.0" encoding="UTF-8"?>' + xml_nfe
                                                        break
                                                except:
                                                    continue
                                    
                                    # Se ainda não encontrou nada, usa o XML completo da resposta
                                    if not xml_nfe:
                                        xml_nfe = xml_content
                                
                            except Exception as parse_error:
                                xml_error_msg = f"Erro ao parsear XML: {str(parse_error)}"
                    
                    # Tenta acessar como objeto normal (se não for Response)
                    if not xml_nfe and resposta_dist and not isinstance(resposta_dist, requests.Response):
                        if hasattr(resposta_dist, 'listaNFe') and resposta_dist.listaNFe:
                            if len(resposta_dist.listaNFe) > 0:
                                xml_nfe = getattr(resposta_dist.listaNFe[0], 'xml', None)
                        elif hasattr(resposta_dist, 'xml'):
                            xml_nfe = resposta_dist.xml
                    
                    # Se conseguiu o XML, extrai os dados
                    if xml_nfe:
                        # Garante que o XML está como string
                        if not isinstance(xml_nfe, str):
                            xml_nfe = str(xml_nfe)
                        
                        # Remove espaços em branco no início/fim
                        xml_nfe = xml_nfe.strip()
                        
                        # Verifica se o XML contém a estrutura básica esperada
                        # Se não tem a declaração XML, adiciona
                        if not xml_nfe.startswith('<?xml'):
                            # Verifica se tem pelo menos um elemento raiz
                            if xml_nfe.startswith('<'):
                                xml_nfe = '<?xml version="1.0" encoding="UTF-8"?>' + xml_nfe
                        
                        # Debug temporário: verifica o que está no XML
                        debug_info = {
                            'xml_length': len(xml_nfe),
                            'has_docZip': 'docZip' in xml_content if 'xml_content' in locals() else False,
                            'has_dest': '<dest' in xml_nfe or 'dest' in xml_nfe,
                            'has_infNFe': '<infNFe' in xml_nfe or 'infNFe' in xml_nfe,
                            'has_xNome': '<xNome' in xml_nfe or 'xNome' in xml_nfe,
                            'xml_preview': xml_nfe[:500] if len(xml_nfe) > 500 else xml_nfe
                        }
                        
                        # Chama a função de extração
                        dados_nfe = extract_nfe_data_from_xml(xml_nfe)
                        
                        # Adiciona debug se não encontrou dados
                        if not dados_nfe.get('customer_name') or dados_nfe.get('customer_name') == "Nome não encontrado no XML":
                            dados_nfe['_debug'] = debug_info
                        return {
                            "status": str(status_code),
                            "motivo": str(motivo) if motivo else "Nota autorizada",
                            "nfe_key": nfe_key,
                            "uf_detectada": uf_da_chave,
                            "uf_usada": cert_config["uf"],
                            "xml_available": True,
                            **dados_nfe
                        }
                except Exception as xml_error:
                    xml_error_msg = str(xml_error)
                
                # Última tentativa: tenta extrair dados básicos do XML da resposta da consulta
                dados_basicos = {}
                if xml_resposta_consulta:
                    try:
                        root_consulta = etree.fromstring(xml_resposta_consulta.encode('utf-8'))
                        # Tenta extrair informações básicas que podem estar na resposta
                        # Algumas respostas de consulta podem ter dados do protocolo
                        for elem in root_consulta.iter():
                            tag = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                            # Procura por informações úteis na resposta
                            if tag.lower() in ['cnpj', 'cpf'] and elem.text and len(elem.text.strip()) >= 11:
                                dados_basicos['tax_id'] = elem.text.strip()
                    except:
                        pass
                
                # Se não conseguiu extrair XML, retorna erro indicando que a nota precisa ser carregada no storage
                error_message = "Nota autorizada (status 100), mas o XML completo não está disponível via SEFAZ. "
                
                if xml_error_msg and ("656" in xml_error_msg or "Consumo Indevido" in xml_error_msg):
                    error_message += "Erro 656 (Consumo Indevido): O certificado não tem permissão para consultar o XML completo desta nota. "
                    error_message += "Para obter os dados, use o endpoint POST /nfe/parse-xml com o XML completo da nota."
                else:
                    error_message += "Para obter os dados, use o endpoint POST /nfe/parse-xml com o XML completo da nota."
                
                raise HTTPException(
                    status_code=404,
                    detail=error_message
                )
            else:
                # Nota não autorizada ou com outro status
                raise HTTPException(
                    status_code=404,
                    detail=f"Nota não encontrada ou não autorizada. Status: {status_code if status_code else 'Desconhecido'}, "
                           f"Motivo: {motivo if motivo else 'Status desconhecido'}. "
                           f"Para consultar esta nota, use o endpoint POST /nfe/parse-xml com o XML completo."
                )
                
        except Exception as consulta_error:
            # Se consulta_nota falhar, tenta consulta_distribuicao para obter o XML
            try:
                # Extrai CNPJ da chave de acesso (posições 6 a 19)
                cnpj_chave = nfe_key[6:20]
                
                resposta_dist = con.consulta_distribuicao(
                    cnpj=cnpj_chave,
                    chave=nfe_key
                )
                
                xml_nfe = None
                
                # Se a resposta é um objeto Response do requests, processa o XML
                if isinstance(resposta_dist, requests.Response):
                    xml_content = resposta_dist.text
                    if xml_content:
                        # Tenta fazer parsing do XML para encontrar a nota
                        root = etree.fromstring(xml_content.encode('utf-8'))
                        # Procura por elementos de NF-e no XML
                        nfe_elements = root.findall('.//NFe') or root.findall('.//nfe:NFe', namespaces={'nfe': 'http://www.portalfiscal.inf.br/nfe'})
                        if nfe_elements:
                            xml_nfe = etree.tostring(nfe_elements[0], encoding='unicode')
                
                # Tenta acessar como objeto normal
                if not xml_nfe and resposta_dist:
                    if hasattr(resposta_dist, 'listaNFe') and resposta_dist.listaNFe:
                        if len(resposta_dist.listaNFe) > 0:
                            xml_nfe = resposta_dist.listaNFe[0].xml
                    elif hasattr(resposta_dist, 'xml'):
                        xml_nfe = resposta_dist.xml
                
                # Se encontrou o XML, extrai os dados
                if xml_nfe:
                    dados_nfe = extract_nfe_data_from_xml(xml_nfe)
                    return {
                        "status": "100",
                        "motivo": "Nota encontrada na distribuição DF-e",
                        "nfe_key": nfe_key,
                        "uf_detectada": uf_da_chave,
                        "uf_usada": cert_config["uf"],
                        "xml_available": True,
                        **dados_nfe
                    }
                
                # Distribuição não retornou nota
                raise Exception(f"Distribuição não encontrou a nota. Tipo: {type(resposta_dist)}, Status HTTP: {resposta_dist.status_code if isinstance(resposta_dist, requests.Response) else 'N/A'}")
            except Exception as dist_error:
                # Se ambos falharem, retorna erro detalhado
                error_detail = (
                    f"Erro na consulta_nota: {str(consulta_error)}. "
                    f"Erro na consulta_distribuicao: {str(dist_error)}"
                )
                raise Exception(error_detail)
    except FileNotFoundError as e:
        # Certificado não encontrado
        raise HTTPException(
            status_code=500,
            detail=f"Certificado não encontrado: {cert_config['path']}. Verifique se o arquivo está na pasta do projeto."
        )
    except Exception as e:
        # Erro ao abrir certificado ou consultar SEFAZ
        error_msg = str(e)
        
        # Mensagens mais amigáveis para erros comuns
        if "senha" in error_msg.lower() or "password" in error_msg.lower():
            error_detail = f"Erro ao abrir certificado: Senha incorreta ou certificado inválido. Detalhes: {error_msg}"
        elif "certificado" in error_msg.lower() or "certificate" in error_msg.lower():
            error_detail = f"Erro no certificado: {error_msg}"
        else:
            error_detail = f"Erro ao consultar SEFAZ: {error_msg}"
        
        # Se houver erro na consulta, retorna erro HTTP apropriado
        # Não retorna dados mockados - a nota precisa ser carregada no storage primeiro
        raise HTTPException(
            status_code=404,
            detail=f"Erro ao consultar SEFAZ: {error_detail}. "
                   f"Para consultar esta nota, use o endpoint POST /nfe/parse-xml com o XML completo da nota."
        )

if __name__ == "__main__":
    # Rodando no localhost na porta 8000
    uvicorn.run(app, host="127.0.0.1", port=8000)