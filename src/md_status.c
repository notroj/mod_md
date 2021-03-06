/* Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include <assert.h>
#include <stdlib.h>

#include <apr_lib.h>
#include <apr_strings.h>
#include <apr_tables.h>
#include <apr_time.h>
#include <apr_date.h>

#include "md_json.h"
#include "md.h"
#include "md_crypt.h"
#include "md_log.h"
#include "md_store.h"
#include "md_result.h"
#include "md_reg.h"
#include "md_util.h"
#include "md_status.h"

/**************************************************************************************************/
/* certificate status information */

static apr_status_t status_get_cert_json(md_json_t **pjson, md_cert_t *cert, apr_pool_t *p)
{
    char ts[APR_RFC822_DATE_LEN];
    const char *finger, *hex;
    apr_status_t rv = APR_SUCCESS;
    apr_array_header_t *scts;
    int i;
    const md_sct *sct;
    md_json_t *sctj, *json;
    
    json = md_json_create(p);
    apr_rfc822_date(ts, md_cert_get_not_before(cert));
    md_json_sets(ts, json, MD_KEY_VALID_FROM, NULL);
    apr_rfc822_date(ts, md_cert_get_not_after(cert));
    md_json_sets(ts, json, MD_KEY_VALID_UNTIL, NULL);
    md_json_sets(md_cert_get_serial_number(cert, p), json, MD_KEY_SERIAL, NULL);
    if (APR_SUCCESS != (rv = md_cert_to_sha256_fingerprint(&finger, cert, p))) goto leave;
    md_json_sets(finger, json, MD_KEY_SHA256_FINGERPRINT, NULL);

    scts = apr_array_make(p, 5, sizeof(const md_sct*));
    if (APR_SUCCESS == md_cert_get_ct_scts(scts, p, cert)) {
        for (i = 0; i < scts->nelts; ++i) {
            sct = APR_ARRAY_IDX(scts, i, const md_sct*);
            sctj = md_json_create(p);
            
            apr_rfc822_date(ts, sct->timestamp);
            md_json_sets(ts, sctj, "signed", NULL);
            md_json_setl(sct->version, sctj, MD_KEY_VERSION, NULL);
            md_data_to_hex(&hex, 0, p, sct->logid);
            md_json_sets(hex, sctj, "logid", NULL);
            md_data_to_hex(&hex, 0, p, sct->signature);
            md_json_sets(hex, sctj, "signature", NULL);
            md_json_sets(md_nid_get_sname(sct->signature_type_nid), sctj, "signature-type", NULL);
            md_json_addj(sctj, json, "scts", NULL);
        }
    }
leave:
    *pjson = (APR_SUCCESS == rv)? json : NULL;
    return rv;
}

/**************************************************************************************************/
/* md status information */

static apr_status_t get_staging_cert_json(md_json_t **pjson, apr_pool_t *p, 
                                          md_reg_t *reg, const md_t *md)
{ 
    md_json_t *json = NULL;
    apr_array_header_t *certs;
    md_cert_t *cert;
    apr_status_t rv = APR_SUCCESS;
    
    rv = md_pubcert_load(md_reg_store_get(reg), MD_SG_STAGING, md->name, &certs, p);
    if (APR_STATUS_IS_ENOENT(rv) || certs->nelts == 0) {
        rv = APR_SUCCESS;
        goto leave;
    }
    else if (APR_SUCCESS != rv) {
        goto leave;
    }
    cert = APR_ARRAY_IDX(certs, 0, md_cert_t *);
    rv = status_get_cert_json(&json, cert, p);
leave:
    *pjson = (APR_SUCCESS == rv)? json : NULL;
    return rv;
}

apr_status_t md_status_get_md_json(md_json_t **pjson, const md_t *md, 
                                   md_reg_t *reg, apr_pool_t *p)
{
    md_json_t *mdj, *jobj, *certj;
    int renew;
    apr_status_t rv = APR_SUCCESS;

    mdj = md_to_json(md, p);
    renew = md_should_renew(md);
    md_json_setb(renew, mdj, MD_KEY_RENEW, NULL);
    if (renew) {
        rv = md_status_job_loadj(&jobj, md->name, reg, p);
        if (APR_SUCCESS == rv) {
            rv = get_staging_cert_json(&certj, p, reg, md);
            if (APR_SUCCESS != rv) goto leave;
            if (certj) md_json_setj(certj, jobj, MD_KEY_CERT, NULL);
            md_json_setj(jobj, mdj, MD_KEY_RENEWAL, NULL);
        }
        else if (APR_STATUS_IS_ENOENT(rv)) rv = APR_SUCCESS;
        else goto leave;
    }
leave:
    *pjson = (APR_SUCCESS == rv)? mdj : NULL;
    return rv;
}

apr_status_t md_status_get_json(md_json_t **pjson, apr_array_header_t *mds, 
                                md_reg_t *reg, apr_pool_t *p) 
{
    md_json_t *json, *mdj;
    apr_status_t rv = APR_SUCCESS;
    const md_t *md;
    int i;
    
    json = md_json_create(p);
    md_json_sets(MOD_MD_VERSION, json, MD_KEY_VERSION, NULL);
    for (i = 0; i < mds->nelts; ++i) {
        md = APR_ARRAY_IDX(mds, i, const md_t *);
        rv = md_status_get_md_json(&mdj, md, reg, p);
        if (APR_SUCCESS != rv) goto leave;
        md_json_addj(mdj, json, MD_KEY_MDS, NULL);
    }
leave:
    *pjson = (APR_SUCCESS == rv)? json : NULL;
    return rv;
}

/**************************************************************************************************/
/* drive job persistence */

static void md_status_job_from_json(md_status_job_t *job, const md_json_t *json, apr_pool_t *p)
{
    const char *s;
    /* not good, this is malloced from a temp pool */
    /*job->name = md_json_gets(json, MD_KEY_NAME, NULL);*/
    job->finished = md_json_getb(json, MD_KEY_FINISHED, NULL);
    s = md_json_dups(p, json, MD_KEY_NEXT_RUN, NULL);
    if (s && *s) job->next_run = apr_date_parse_rfc(s);
    s = md_json_dups(p, json, MD_KEY_VALID_FROM, NULL);
    if (s && *s) job->valid_from = apr_date_parse_rfc(s);
    job->notified = md_json_getb(json, MD_KEY_NOTIFIED, NULL);
    job->error_runs = (int)md_json_getl(json, MD_KEY_ERRORS, NULL);
    if (md_json_has_key(json, MD_KEY_LAST, NULL)) {
        job->last_result = md_result_from_json(md_json_getcj(json, MD_KEY_LAST, NULL), p);
    }
}

void md_status_job_to_json(md_json_t *json, const md_status_job_t *job, apr_pool_t *p)
{
    char ts[APR_RFC822_DATE_LEN];

    md_json_sets(job->name, json, MD_KEY_NAME, NULL);
    md_json_setb(job->finished, json, MD_KEY_FINISHED, NULL);
    if (job->next_run > 0) {
        apr_rfc822_date(ts, job->next_run);
        md_json_sets(ts, json, MD_KEY_NEXT_RUN, NULL);
    }
    if (job->valid_from > 0) {
        apr_rfc822_date(ts, job->valid_from);
        md_json_sets(ts, json, MD_KEY_VALID_FROM, NULL);
    }
    md_json_setb(job->notified, json, MD_KEY_NOTIFIED, NULL);
    md_json_setl(job->error_runs, json, MD_KEY_ERRORS, NULL);
    if (job->last_result) {
        md_json_setj(md_result_to_json(job->last_result, p), json, MD_KEY_LAST, NULL);
    }
}

apr_status_t md_status_job_loadj(md_json_t **pjson, const char *name, 
                                struct md_reg_t *reg, apr_pool_t *p)
{
    md_store_t *store = md_reg_store_get(reg);
    return md_store_load_json(store, MD_SG_STAGING, name, MD_FN_JOB, pjson, p);
}

apr_status_t md_status_job_load(md_status_job_t *job, md_reg_t *reg, apr_pool_t *p)
{
    md_store_t *store = md_reg_store_get(reg);
    md_json_t *jprops;
    apr_status_t rv;
    
    rv = md_store_load_json(store, MD_SG_STAGING, job->name, MD_FN_JOB, &jprops, p);
    if (APR_SUCCESS == rv) {
        md_status_job_from_json(job, jprops, p);
        job->dirty = 0;
    }
    return rv;
}

apr_status_t md_status_job_save(md_status_job_t *job, md_reg_t *reg, apr_pool_t *p)
{
    md_store_t *store = md_reg_store_get(reg);
    md_json_t *jprops;
    apr_status_t rv;
    
    jprops = md_json_create(p);
    md_status_job_to_json(jprops, job, p);
    rv = md_store_save_json(store, p, MD_SG_STAGING, job->name, MD_FN_JOB, jprops, 1);
    if (APR_SUCCESS == rv) job->dirty = 0;
    return rv;
}

void  md_status_take_stock(md_status_stock_t *stock, apr_array_header_t *mds, 
                           md_reg_t *reg, apr_pool_t *p)
{
    const md_t *md;
    md_status_job_t job;
    int i;

    memset(stock, 0, sizeof(*stock));
    for (i = 0; i < mds->nelts; ++i) {
        md = APR_ARRAY_IDX(mds, i, const md_t *);
        switch (md->state) {
            case MD_S_COMPLETE: stock->ok_count++; /* fall through */
            case MD_S_INCOMPLETE:
                if (md_should_renew(md)) {
                    stock->renew_count++;
                    memset(&job, 0, sizeof(job));
                    job.name = md->name;
                    if (APR_SUCCESS == md_status_job_load(&job, reg, p)) {
                        if (job.error_runs > 0 
                            || (job.last_result && job.last_result->status != APR_SUCCESS)) {
                            stock->errored_count++;
                        }
                        else if (job.finished) {
                            stock->ready_count++;
                        }
                    }
                }
                break;
            default: stock->errored_count++; break;
        }
    }
}


