# SefazBridge

Microserviço Python (FastAPI) para processamento de Notas Fiscais Eletrônicas (NF-e) da SEFAZ. Permite parse local de XMLs de NF-e e integração com webservices da SEFAZ para consulta de notas.

## 🚀 Funcionalidades

- **Parse Local de XML**: Extração de dados de NF-e a partir de XML completo sem necessidade de consulta à SEFAZ
- **Integração SEFAZ**: Consulta de notas fiscais via webservices da SEFAZ usando certificado digital A1
- **Upload de Certificado**: Upload e configuração de certificado digital (.pfx) via API
- **Detecção Automática de UF**: Identifica automaticamente o estado da nota a partir da chave de acesso
- **Extração de Dados**: Extrai nome do destinatário, endereço, CNPJ/CPF e valor total da nota

## 📋 Requisitos

- Python 3.8+
- Certificado Digital A1 (.pfx) - opcional (apenas para consulta SEFAZ)

## 🔧 Instalação

1. Clone o repositório:
```bash
git clone <url-do-repositorio>
cd SefazBridge
```

2. Crie um ambiente virtual:
```bash
python -m venv venv
```

3. Ative o ambiente virtual:

**Windows:**
```bash
venv\Scripts\activate
```

**Linux/Mac:**
```bash
source venv/bin/activate
```

4. Instale as dependências:
```bash
pip install -r requirements.txt
```

## ⚙️ Configuração

### Opção 1: Arquivo .env (Recomendado)

Crie um arquivo `.env` na raiz do projeto:

```env
CERT_PATH=certificados/seu_certificado.pfx
CERT_PASSWORD=sua_senha_aqui
UF=SP
HOMOLOGACAO=false
```

### Opção 2: Upload via API

Use o endpoint `POST /upload-certificate` para fazer upload do certificado.

## 🏃 Executando

```bash
python main.py
```

O servidor estará disponível em `http://127.0.0.1:8000`

Documentação interativa da API: `http://127.0.0.1:8000/docs`

## 📡 Endpoints da API

### POST `/nfe/parse-xml`

Faz parse local do XML da NF-e e extrai dados do destinatário.

**Request:**
```json
{
  "xml": "<?xml version=\"1.0\" encoding=\"UTF-8\"?>..."
}
```

**Response:**
```json
{
  "name": "Nome do Destinatário",
  "address": "Rua Exemplo, 123 - Bairro - Cidade/UF",
  "taxId": "123.456.789-00"
}
```

**Exemplo com cURL:**
```bash
curl -X POST "http://127.0.0.1:8000/nfe/parse-xml" \
  -H "Content-Type: application/json" \
  -d '{"xml": "<?xml version=\"1.0\"?>..."}'
```

### POST `/upload-certificate`

Faz upload do certificado digital A1 (.pfx).

**Request (multipart/form-data):**
- `file`: Arquivo .pfx
- `password`: Senha do certificado
- `uf`: Sigla do estado (SP, MG, RJ, etc.)
- `homologacao`: true/false (opcional, default: false)

**Response:**
```json
{
  "message": "Certificado carregado e validado com sucesso!",
  "filename": "certificado.pfx",
  "uf": "SP",
  "homologacao": false,
  "path": "/caminho/absoluto/certificado.pfx"
}
```

**Exemplo com cURL:**
```bash
curl -X POST "http://127.0.0.1:8000/upload-certificate" \
  -F "file=@certificado.pfx" \
  -F "password=senha123" \
  -F "uf=SP" \
  -F "homologacao=false"
```

### GET `/nfe/{nfe_key}`

Consulta NF-e na SEFAZ pela chave de acesso (44 dígitos).

**Exemplo:**
```bash
curl "http://127.0.0.1:8000/nfe/35251147350742000100550030000217731388003572"
```

**Nota:** Este endpoint requer certificado configurado e permissões adequadas na SEFAZ. Se não houver integração configurada, retorna erro 404 sugerindo o uso de `POST /nfe/parse-xml`.

### GET `/certificate/status`

Retorna o status atual do certificado configurado.

**Response:**
```json
{
  "configured": true,
  "path": "/caminho/certificado.pfx",
  "path_exists": true,
  "uf": "SP",
  "homologacao": false,
  "source": "upload"
}
```

### DELETE `/certificate`

Remove o certificado enviado via upload (volta para configuração do .env).

## 📦 Estrutura do Projeto

```
SefazBridge/
├── main.py                 # Aplicação FastAPI principal
├── requirements.txt        # Dependências do projeto
├── .env                    # Configurações (não versionado)
├── .gitignore             # Arquivos ignorados pelo Git
├── certificates/          # Diretório para certificados enviados
└── README.md              # Este arquivo
```

## 🔒 Segurança

- Certificados e senhas não são versionados no Git
- O diretório `certificates/` está no `.gitignore`
- Arquivo `.env` está no `.gitignore`
- Em produção, considere usar variáveis de ambiente seguras ou um gerenciador de segredos

## 🛠️ Tecnologias Utilizadas

- **FastAPI**: Framework web moderno e rápido para APIs
- **pynfe**: Biblioteca para integração com webservices da SEFAZ
- **lxml**: Parser XML eficiente
- **python-decouple**: Gerenciamento de configurações
- **cryptography**: Suporte a certificados digitais
- **uvicorn**: Servidor ASGI de alta performance

## 📝 Notas Importantes

- O endpoint `GET /nfe/{nfe_key}` requer certificado válido e pode retornar erro 656 (Consumo Indevido) se o certificado não tiver permissão para consultar a nota
- Para processar XMLs já armazenados, use sempre `POST /nfe/parse-xml`
- A detecção automática de UF funciona apenas para NF-e (44 dígitos), não para NFS-e
- Certificados de empresa (ME) podem não ter permissão para consultar notas onde a pessoa física é o destinatário

## 🤝 Contribuindo

Contribuições são bem-vindas! Sinta-se à vontade para abrir issues ou pull requests.

## 📄 Licença

Este projeto é de uso interno.

## 📞 Suporte

Para dúvidas ou problemas, abra uma issue no repositório.
